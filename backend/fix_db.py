import sqlite3
c = sqlite3.connect("epl.db")
c.execute("ALTER TABLE other_fixtures ADD COLUMN source TEXT DEFAULT 'manual'")
c.commit()
print("done")
