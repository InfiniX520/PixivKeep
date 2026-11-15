"""Measure what a held handle actually protects on this host; synthetic only."""
import ctypes, os, sqlite3, tempfile
from pathlib import Path
from ctypes import wintypes


def attempt(label, fn):
    try:
        value = fn()
        print("%-42s OK        %s" % (label, value))
    except OSError as error:
        print("%-42s BLOCKED   %s/%s" % (label, type(error).__name__,
                                          getattr(error, "winerror", error.errno)))
    except Exception as error:
        print("%-42s ERROR     %s/%s" % (label, type(error).__name__, error))


base = Path(tempfile.mkdtemp(prefix="probe_lock_"))
db = base / "target.sqlite3"
victim = base / "victim.sqlite3"
victim.write_bytes(bytes(8192))
with sqlite3.connect(db) as c:
    c.execute("CREATE TABLE t(v)")
    c.execute("INSERT INTO t VALUES (1)")
db_before = db.stat().st_size

handle = os.open(db, os.O_RDWR)
print("held handle on %s (size %d)" % (db.name, db_before))
attempt("unlink name while handle held", lambda: os.unlink(db))
replacement = base / "replacement.sqlite3"
replacement.write_bytes(b"replacement")
attempt("os.replace onto held name", lambda: os.replace(replacement, db))


def link_after_unlink():
    os.unlink(db)
    os.link(victim, db)


attempt("unlink+hardlink victim onto held name", link_after_unlink)


def sqlite_write_through_name():
    with sqlite3.connect(str(db), isolation_level=None) as c:
        c.execute("PRAGMA journal_mode=DELETE")
        c.execute("INSERT INTO t VALUES (2)")
    return db.stat().st_size


attempt("sqlite3 write through held name", sqlite_write_through_name)


def sqlite_backup_into_held_name():
    dest = base / "snapshot.sqlite3"
    dest.write_bytes(b"")
    h2 = os.open(dest, os.O_RDWR)
    try:
        with sqlite3.connect(str(dest), isolation_level=None) as out:
            with sqlite3.connect(str(db)) as src:
                src.backup(out)
            out.execute("PRAGMA journal_mode=DELETE")
        return dest.stat().st_size
    finally:
        os.close(h2)


attempt("sqlite3 backup into held empty file", sqlite_backup_into_held_name)
os.close(handle)
attempt("unlink after close", lambda: os.unlink(db))

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
K32 = ctypes.WinDLL("kernel32", use_last_error=True)
K32.CreateFileW.restype = wintypes.HANDLE


def open_dir_handle(path, share_delete):
    share = FILE_SHARE_READ | FILE_SHARE_WRITE | (FILE_SHARE_DELETE if share_delete else 0)
    handle = K32.CreateFileW(str(path), GENERIC_READ, share, None, OPEN_EXISTING,
                             FILE_FLAG_BACKUP_SEMANTICS, None)
    if handle == wintypes.HANDLE(-1).value or handle == -1:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


parent = base / "workspace"
parent.mkdir()
ph = open_dir_handle(parent, share_delete=False)
parent2 = base / "workspace-renamed"
attempt("rename dir while dir handle held", lambda: os.rename(parent, parent2))
attempt("create child while dir handle held",
        lambda: ((parent / "child.bin").write_bytes(b"x"), "created")[1])
attempt("delete child while dir handle held", lambda: os.unlink(parent / "child.bin"))
K32.CloseHandle(ph)
attempt("rename dir after dir handle closed",
        lambda: (os.rename(parent, parent2), "renamed")[1])
parent3 = base / "workspace2"
parent3.mkdir()
ph2 = open_dir_handle(parent3, share_delete=True)
attempt("rename dir, handle shares delete",
        lambda: (os.rename(parent3, base / "workspace2-renamed"), "renamed")[1])
K32.CloseHandle(ph2)
print("base:", base)
