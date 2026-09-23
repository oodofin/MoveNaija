"""One-time trusted command-line admin bootstrap."""
import sys
from .db import database, init_db

if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python -m app.promote_admin registered-email@example.com')
    init_db()
    with database() as db:
        result = db.execute("UPDATE users SET role='admin' WHERE email=?", (sys.argv[1].lower(),))
        if result.rowcount != 1:
            raise SystemExit('No registered user has that email.')
    print('Administrator role granted.')
