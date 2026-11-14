import csv
import io
from datetime import datetime, timezone

from config import Config
from checker import check, CheckerError
from models import enc


def norm(k: str) -> str:
    return (k or "").replace("\ufeff", "").strip().lower()


REQUIRED_HEADER = {"email", "password"}


def parse_csv_bytes(raw_bytes: bytes):
    raw = raw_bytes.decode("utf-8-sig", errors="replace")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    reader = csv.DictReader(io.StringIO(raw))
    fieldnames = list(reader.fieldnames or [])
    rows = list(reader)
    normalized = {norm(k) for k in fieldnames}
    if not REQUIRED_HEADER.issubset(normalized):
        return fieldnames, None
    return fieldnames, rows


def _prices_for(category: str) -> dict:
    return Config.PLAN_PRICES.get(category, Config.PLAN_PRICES["silver"])


def _resolve_category(api_cat: str, csv_cat: str) -> str:
    csv_cat = (csv_cat or "auto").lower()
    if csv_cat in ("silver", "gold"):
        return csv_cat
    return api_cat or "silver"


def import_one(conn, email, password, *,
               plan_category="auto", cost=0, notes=""):
    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        return False, "missing email or password", {}

    try:
        result = check(email, password)
    except CheckerError as e:
        return False, f"checker: {e}", {}

    if not result["ok"]:
        return False, result["error"] or "check failed", {"raw": result["raw"]}
    if not result["live"]:
        return False, "account not live", {"plan_name": result["plan_name"]}

    category = _resolve_category(result["plan_category"], plan_category)
    prices = _prices_for(category)

    status = "LIVE"
    expiry = result["expiry"]
    if expiry:
        try:
            end_dt = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            status = "LIVE" if end_dt > datetime.now(timezone.utc) else "EXPIRED"
        except Exception:
            status = "LIVE"

    wallet = result["wallet"]
    conn.execute(
        """INSERT INTO accounts
           (email_enc, password_enc, plan_name, plan_category,
            original_price, original_currency, selling_price, cost,
            status, expiry_date, last_sync, notes, fw_balance, fw_currency)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (enc(email), enc(password), result["plan_name"] or f"{category.title()} Plan",
         category, prices["original_price"], prices["original_currency"],
         prices["selling_price"], cost, status, expiry,
         datetime.utcnow().isoformat(timespec="seconds"), notes,
         wallet["balance"], wallet["currency"]),
    )
    info = {
        "plan_name": result["plan_name"],
        "category": category,
        "selling_price": prices["selling_price"],
        "original_price": prices["original_price"],
        "wallet": wallet,
        "status": status,
    }
    return True, None, info


def import_csv_rows(conn, rows):
    added = 0
    skipped = 0
    failed = []
    for i, raw in enumerate(rows, start=2):
        clean = {norm(k): (v or "").strip() for k, v in raw.items()}
        email = clean.get("email")
        password = clean.get("password")
        if not email or not password:
            skipped += 1
            continue
        try:
            cost = float(clean.get("cost") or 0)
        except ValueError:
            cost = 0
        ok, err, _info = import_one(
            conn, email, password,
            plan_category=clean.get("plan_category") or "auto",
            cost=cost,
            notes=clean.get("notes") or "",
        )
        if ok:
            added += 1
        else:
            failed.append(f"row {i} ({email[:24]}): {err}")
    return {"added": added, "skipped": skipped, "failed": failed}


def import_pipe_rows(conn, rows, default_category="silver"):
    """Pipe rows are pre-validated. No checker API. Category from username."""
    added = 0
    failed = []
    for i, r in enumerate(rows, start=1):
        email = r["email"]
        password = r["password"]
        cat = (r.get("plan_category") or default_category).lower()
        if cat not in ("silver", "gold"):
            cat = default_category
        prices = _prices_for(cat)
        try:
            conn.execute(
                """INSERT INTO accounts
                   (email_enc, password_enc, plan_name, plan_category,
                    original_price, original_currency, selling_price, cost,
                    status, expiry_date, last_sync, notes, fw_balance, fw_currency)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (enc(email), enc(password), f"{cat.title()} Plan", cat,
                 prices["original_price"], prices["original_currency"],
                 prices["selling_price"], r.get("cost", 0),
                 "LIVE", None,
                 datetime.utcnow().isoformat(timespec="seconds"),
                 r.get("notes") or "",
                 0.0, "USD"),
            )
            added += 1
        except Exception as e:
            failed.append(f"row {i} ({email[:20]}): {e}")
    return {"added": added, "skipped": 0, "failed": failed}