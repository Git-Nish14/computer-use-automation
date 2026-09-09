# Mock banking portal — intentionally legacy HTML (table layouts, no test IDs)
# to stand in for a real core-banking back-office application.

from __future__ import annotations

import os
from functools import wraps

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

app = Flask(__name__)
app.secret_key = "heritage-dev-secret-not-for-production"

MEMBERS: dict[str, dict] = {
    "12345": {
        "name": "John Smith",
        "dob": "1975-03-15",
        "address": "123 Main St, Springfield, IL 62701",
        "phone": "217-555-0101",
        "accounts": [
            {"type": "Checking", "number": "****1234", "balance": 2500.00, "status": "Active"},
            {"type": "Savings",  "number": "****5678", "balance": 8750.00, "status": "Active"},
        ],
    },
    "67890": {
        "name": "Jane Doe",
        "dob": "1988-07-22",
        "address": "456 Oak Ave, Chicago, IL 60601",
        "phone": "312-555-0202",
        "accounts": [
            {"type": "Checking", "number": "****2345", "balance": 1200.00, "status": "Active"},
            {"type": "Savings",  "number": "****6789", "balance": 3400.00, "status": "Active"},
        ],
    },
    "11111": {
        "name": "Bob Johnson",
        "dob": "1962-11-30",
        "address": "789 Pine Rd, Rockford, IL 61101",
        "phone": "815-555-0303",
        "accounts": [
            {"type": "Savings", "number": "****3456", "balance": 15000.00, "status": "Active"},
        ],
    },
}

CREDENTIALS = {"demo": "demo123"}

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return redirect(url_for("dashboard" if session.get("logged_in") else "login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if CREDENTIALS.get(username) == password:
            session["logged_in"] = True
            session["username"] = username
            return redirect(url_for("dashboard"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", username=session.get("username"))


@app.route("/members/search", methods=["GET", "POST"])
@login_required
def member_search():
    results = None
    query_id = ""
    query_name = ""
    if request.method == "POST":
        query_id = request.form.get("member_id", "").strip()
        query_name = request.form.get("last_name", "").strip()
        results = []
        for mid, m in MEMBERS.items():
            if query_id and mid != query_id:
                continue
            if query_name and query_name.lower() not in m["name"].lower():
                continue
            results.append({"id": mid, **m})
    return render_template(
        "search.html",
        results=results,
        query_id=query_id,
        query_name=query_name,
    )


@app.route("/members/<member_id>")
@login_required
def member_detail(member_id: str):
    member = MEMBERS.get(member_id)
    if not member:
        flash("Member not found.", "error")
        return redirect(url_for("member_search"))
    return render_template("member_detail.html", member_id=member_id, member=member)


@app.route("/members/<member_id>/sub-account", methods=["GET", "POST"])
@login_required
def sub_account_form(member_id: str):
    member = MEMBERS.get(member_id)
    if not member:
        flash("Member not found.", "error")
        return redirect(url_for("member_search"))
    if request.method == "POST":
        account_type = request.form.get("account_type", "")
        initial_deposit = request.form.get("initial_deposit", "")
        purpose = request.form.get("purpose", "")
        return render_template(
            "sub_account_confirm.html",
            member_id=member_id,
            member=member,
            account_type=account_type,
            initial_deposit=initial_deposit,
            purpose=purpose,
        )
    return render_template("sub_account_form.html", member_id=member_id, member=member)


@app.route("/members/<member_id>/sub-account/confirm", methods=["POST"])
@login_required
def sub_account_confirm(member_id: str):
    member = MEMBERS.get(member_id)
    if not member:
        flash("Member not found.", "error")
        return redirect(url_for("member_search"))
    account_type = request.form.get("account_type", "")
    flash(f"Sub-account ({account_type}) opened successfully for {member['name']}.", "success")
    return redirect(url_for("member_detail", member_id=member_id))


if __name__ == "__main__":
    host = os.environ.get("MOCK_APP_HOST", "127.0.0.1")
    port = int(os.environ.get("MOCK_APP_PORT", "5000"))
    app.run(host=host, port=port, debug=False)
