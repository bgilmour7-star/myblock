"""
My Block — a tiny web app for lending tools & equipment with your trusted neighbours.

Production stack (all free-tier friendly):
    - Hosting:  Vercel (Hobby plan) — Flask deploys natively, see vercel.json
    - Database: Postgres via Neon (add it from the project's Storage tab in Vercel)
    - Photos:   Vercel Blob (add it from the same Storage tab)

Local development:
    pip install -r requirements.txt
    export DATABASE_URL="postgres://..."       # a Neon connection string works locally too
    export MY_BLOCK_INVITE_CODE="your-code"
    export MY_BLOCK_SECRET="something-random"
    export BLOB_READ_WRITE_TOKEN="..."          # optional locally; without it, photo uploads are skipped gracefully
    python app.py

See README.md for step-by-step deployment instructions.
"""

import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from flask import Flask, g, redirect, render_template, request, session, url_for, flash
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

try:
    import vercel_blob
except ImportError:  # not installed yet, or running somewhere that doesn't need it
    vercel_blob = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "")
INVITE_CODE = os.environ.get("MY_BLOCK_INVITE_CODE", "changeme")
SECRET_KEY = os.environ.get("MY_BLOCK_SECRET", "dev-secret-change-me")
BLOB_TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN", "")

MAX_PHOTO_BYTES = 4 * 1024 * 1024  # stay under Vercel Functions' 4.5MB request body limit
ALLOWED_PHOTO_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}

# Karma badge tiers: (min_points, label)
BADGE_TIERS = [
    (0, "Newcomer"),
    (1, "Helpful Neighbour"),
    (3, "Generous Neighbour"),
    (6, "Tool Shed Hero"),
    (11, "Block Legend"),
]

# Single source of truth for item categories — used by both the listing form and the
# browse-page filter pills, so the two can never drift out of sync.
CATEGORIES = [
    "Power tools",
    "Hand tools",
    "Yard & garden",
    "Ladders & access",
    "Cleaning",
    "Party & events",
    "Other",
]

# Browse-page sort options: label -> SQL ORDER BY clause. Keyed by a whitelist so the
# query string can never inject arbitrary SQL — only these exact fragments are ever used.
SORT_OPTIONS = {
    "recommended": "CASE items.status WHEN 'available' THEN 0 ELSE 1 END, items.created_at DESC",
    "newest": "items.created_at DESC",
}

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_PHOTO_BYTES + 200 * 1024  # small buffer for form fields


def badge_for(points: int) -> str:
    label = BADGE_TIERS[0][1]
    for threshold, name in BADGE_TIERS:
        if points >= threshold:
            label = name
    return label


app.jinja_env.filters["badge"] = badge_for


# ---------------------------------------------------------------------------
# Database helpers (Postgres via psycopg — works locally and on Vercel Functions)
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Add a Postgres database (e.g. Neon, from the "
                "Vercel Storage tab) and set DATABASE_URL to its connection string."
            )
        g.db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Function instances on Vercel can be reused between requests (warm starts), so we only
# want to run the schema check once per instance rather than on every single request.
_schema_ready = False


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            karma_points INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Migrate a table created before email-based login existed. Safe to run every time:
    # ADD COLUMN/DROP CONSTRAINT/CREATE INDEX are all no-ops once already applied.
    db.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT")
    db.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_name_key")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx ON users (LOWER(email))"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            id SERIAL PRIMARY KEY,
            owner_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Other',
            description TEXT NOT NULL DEFAULT '',
            photo_url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'available',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS loans (
            id SERIAL PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id),
            borrower_id INTEGER NOT NULL REFERENCES users(id),
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            approved_at TIMESTAMPTZ,
            returned_at TIMESTAMPTZ
        )
        """
    )
    db.commit()
    _schema_ready = True


@app.before_request
def _init_schema():
    ensure_schema()


# ---------------------------------------------------------------------------
# Auth — real accounts: name + password, gated behind a shared street invite code at signup
# ---------------------------------------------------------------------------
def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()


@app.before_request
def require_login():
    open_endpoints = {"login", "signup", "static"}
    if request.endpoint not in open_endpoints and current_user() is None:
        return redirect(url_for("login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        invite = request.form.get("invite", "").strip()

        if not name or not email or not password:
            flash("Enter your name, email, and a password.", "error")
            return render_template("signup.html")

        if "@" not in email:
            flash("That doesn't look like a valid email address.", "error")
            return render_template("signup.html")

        if invite != INVITE_CODE:
            flash("Wrong invite code — check with whoever set this up.", "error")
            return render_template("signup.html")

        db = get_db()
        existing = db.execute(
            "SELECT id FROM users WHERE LOWER(email) = LOWER(%s)", (email,)
        ).fetchone()
        if existing:
            flash("An account with that email already exists — log in instead.", "error")
            return render_template("signup.html")

        db.execute(
            "INSERT INTO users (name, email, password_hash, karma_points) VALUES (%s, %s, %s, 0)",
            (name, email, generate_password_hash(password)),
        )
        db.commit()
        user = db.execute(
            "SELECT * FROM users WHERE LOWER(email) = LOWER(%s)", (email,)
        ).fetchone()
        session["user_id"] = user["id"]
        return redirect(url_for("browse_items"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE LOWER(email) = LOWER(%s)", (email,)
        ).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("That email/password combination doesn't match.", "error")
            return render_template("login.html")

        session["user_id"] = user["id"]
        return redirect(url_for("browse_items"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Photo uploads (Vercel Blob)
# ---------------------------------------------------------------------------
def upload_photo(file_storage):
    """Returns a photo URL string, "" if no photo was given, or None on a validation error
    (an error message has already been flashed in that case)."""
    if not file_storage or not file_storage.filename:
        return ""

    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_PHOTO_EXTS:
        flash("Photo must be a png, jpg, gif, or webp.", "error")
        return None

    data = file_storage.read()
    if len(data) > MAX_PHOTO_BYTES:
        flash("Photo is too big — keep it under 4MB.", "error")
        return None

    if vercel_blob is None or not BLOB_TOKEN:
        # Don't hard-fail — just save the item without a photo so local dev without
        # Blob configured still works.
        flash('Item saved, but photo uploads aren\'t configured yet (missing BLOB_READ_WRITE_TOKEN).', "error")
        return ""

    safe_name = secure_filename(file_storage.filename) or "photo"
    timestamp = int(datetime.now(timezone.utc).timestamp())
    pathname = f"items/{timestamp}-{safe_name}"

    result = vercel_blob.put(pathname, data, {"addRandomSuffix": "true"})
    return result.get("url", "")


# ---------------------------------------------------------------------------
# Browse & lend
# ---------------------------------------------------------------------------
@app.route("/")
@app.route("/items")
def browse_items():
    db = get_db()
    user = current_user()

    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    if category not in CATEGORIES:
        category = ""
    available_only = request.args.get("available") == "1"
    sort = request.args.get("sort", "recommended")
    if sort not in SORT_OPTIONS:
        sort = "recommended"

    conditions = []
    params = []
    if q:
        conditions.append("(items.name ILIKE %s OR items.description ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    if category:
        conditions.append("items.category = %s")
        params.append(category)
    if available_only:
        conditions.append("items.status = 'available'")
    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    items = db.execute(
        f"""
        SELECT items.*, users.name AS owner_name, users.karma_points AS owner_karma
        FROM items
        JOIN users ON users.id = items.owner_id
        {where_sql}
        ORDER BY {SORT_OPTIONS[sort]}
        """,
        params,
    ).fetchall()

    return render_template(
        "items.html",
        items=items,
        user=user,
        categories=CATEGORIES,
        q=q,
        selected_category=category,
        available_only=available_only,
        sort=sort,
        filters_active=bool(q or category or available_only),
    )


@app.route("/items/<int:item_id>")
def item_detail(item_id):
    db = get_db()
    user = current_user()
    item = db.execute(
        """
        SELECT items.*, users.name AS owner_name, users.karma_points AS owner_karma
        FROM items
        JOIN users ON users.id = items.owner_id
        WHERE items.id = %s
        """,
        (item_id,),
    ).fetchone()
    if item is None:
        flash("That item doesn't exist anymore.", "error")
        return redirect(url_for("browse_items"))
    return render_template("item_detail.html", item=item, user=user)


@app.route("/items/new", methods=["GET", "POST"])
def new_item():
    user = current_user()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        category = request.form.get("category", "Other").strip()
        if category not in CATEGORIES:
            category = "Other"
        description = request.form.get("description", "").strip()

        if not name:
            flash("Give the item a name.", "error")
            return render_template("item_form.html", categories=CATEGORIES)

        photo_url = upload_photo(request.files.get("photo"))
        if photo_url is None:
            return render_template("item_form.html", categories=CATEGORIES)

        db = get_db()
        db.execute(
            """INSERT INTO items (owner_id, name, category, description, photo_url, status)
               VALUES (%s, %s, %s, %s, %s, 'available')""",
            (user["id"], name, category, description, photo_url),
        )
        db.commit()
        flash(f'"{name}" is now listed for the block to borrow.', "success")
        return redirect(url_for("browse_items"))

    return render_template("item_form.html", categories=CATEGORIES)


@app.route("/items/<int:item_id>/request", methods=["POST"])
def request_item(item_id):
    user = current_user()
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = %s", (item_id,)).fetchone()

    if item is None:
        flash("That item doesn't exist anymore.", "error")
    elif item["owner_id"] == user["id"]:
        flash("You can't borrow your own item.", "error")
    elif item["status"] != "available":
        flash("Someone already has dibs on that one.", "error")
    else:
        db.execute(
            "INSERT INTO loans (item_id, borrower_id, status) VALUES (%s, %s, 'pending')",
            (item_id, user["id"]),
        )
        db.execute("UPDATE items SET status = 'requested' WHERE id = %s", (item_id,))
        db.commit()
        flash(f'Requested "{item["name"]}" — the owner will see it on their My Items page.', "success")

    return redirect(url_for("browse_items"))


# ---------------------------------------------------------------------------
# My stuff: items I own + requests to approve, loans I've requested
# ---------------------------------------------------------------------------
@app.route("/my-items")
def my_items():
    user = current_user()
    db = get_db()

    owned_items = db.execute(
        "SELECT * FROM items WHERE owner_id = %s ORDER BY created_at DESC", (user["id"],)
    ).fetchall()

    pending_requests = db.execute(
        """
        SELECT loans.*, items.name AS item_name, users.name AS borrower_name
        FROM loans
        JOIN items ON items.id = loans.item_id
        JOIN users ON users.id = loans.borrower_id
        WHERE items.owner_id = %s AND loans.status IN ('pending', 'approved')
        ORDER BY loans.requested_at DESC
        """,
        (user["id"],),
    ).fetchall()

    my_borrow_requests = db.execute(
        """
        SELECT loans.*, items.name AS item_name, users.name AS owner_name
        FROM loans
        JOIN items ON items.id = loans.item_id
        JOIN users ON users.id = items.owner_id
        WHERE loans.borrower_id = %s AND loans.status IN ('pending', 'approved')
        ORDER BY loans.requested_at DESC
        """,
        (user["id"],),
    ).fetchall()

    return render_template(
        "my_items.html",
        user=user,
        owned_items=owned_items,
        pending_requests=pending_requests,
        my_borrow_requests=my_borrow_requests,
    )


def _owned_loan(db, loan_id, user_id):
    """Fetch a loan (with its item) only if user_id owns the item it's for."""
    loan = db.execute(
        """
        SELECT loans.*, items.owner_id AS item_owner_id
        FROM loans
        JOIN items ON items.id = loans.item_id
        WHERE loans.id = %s
        """,
        (loan_id,),
    ).fetchone()
    if loan is None or loan["item_owner_id"] != user_id:
        return None
    return loan


@app.route("/loans/<int:loan_id>/approve", methods=["POST"])
def approve_loan(loan_id):
    user = current_user()
    db = get_db()
    loan = _owned_loan(db, loan_id, user["id"])
    if loan is None:
        flash("That request isn't yours to approve.", "error")
    else:
        db.execute(
            "UPDATE loans SET status = 'approved', approved_at = now() WHERE id = %s",
            (loan_id,),
        )
        db.execute("UPDATE items SET status = 'on_loan' WHERE id = %s", (loan["item_id"],))
        db.commit()
        flash("Loan approved — enjoy the karma once it's back!", "success")
    return redirect(url_for("my_items"))


@app.route("/loans/<int:loan_id>/decline", methods=["POST"])
def decline_loan(loan_id):
    user = current_user()
    db = get_db()
    loan = _owned_loan(db, loan_id, user["id"])
    if loan is None:
        flash("That request isn't yours to decline.", "error")
    else:
        db.execute("UPDATE loans SET status = 'declined' WHERE id = %s", (loan_id,))
        db.execute("UPDATE items SET status = 'available' WHERE id = %s", (loan["item_id"],))
        db.commit()
        flash("Request declined.", "success")
    return redirect(url_for("my_items"))


@app.route("/loans/<int:loan_id>/return", methods=["POST"])
def return_loan(loan_id):
    user = current_user()
    db = get_db()
    loan = _owned_loan(db, loan_id, user["id"])
    if loan is None:
        flash("That loan isn't yours to mark returned.", "error")
    else:
        db.execute(
            "UPDATE loans SET status = 'returned', returned_at = now() WHERE id = %s",
            (loan_id,),
        )
        db.execute("UPDATE items SET status = 'available' WHERE id = %s", (loan["item_id"],))
        db.execute(
            "UPDATE users SET karma_points = karma_points + 1 WHERE id = %s",
            (loan["item_owner_id"],),
        )
        db.commit()
        flash("Marked returned — a karma point just went to the lender. Nice.", "success")
    return redirect(url_for("my_items"))


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------
@app.route("/leaderboard")
def leaderboard():
    db = get_db()
    users = db.execute(
        "SELECT * FROM users ORDER BY karma_points DESC, name ASC"
    ).fetchall()
    return render_template("leaderboard.html", users=users, user=current_user())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
