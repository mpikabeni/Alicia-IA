#!/usr/bin/env python3
"""
import_dump.py - recharge un dump PostgreSQL (format "directory", celui que
Render te donne en .tar.gz) dans la base utilisée par bot.py.

Usage :
    python import_dump.py 2026-10-26T20_26Z_dir_tar.gz --dry-run   # simulation
    python import_dump.py 2026-10-26T20_26Z_dir_tar.gz             # import réel

La destination est choisie comme dans bot.py :
  - variable DATABASE_URL définie  -> PostgreSQL (psycopg)
  - sinon                          -> SQLite (fichier ALICIA_DB, défaut alicia_v3.db)

Par défaut les lignes déjà présentes dans la base ne sont PAS écrasées
(ON CONFLICT DO NOTHING). Option --wipe : vide chaque table avant import.
"""
import argparse
import os
import re
import sqlite3
import sys
import tarfile
import tempfile

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = os.getenv("ALICIA_DB", "alicia_v3.db").strip()

# Tables techniques à ne pas réimporter (inutiles ou dangereuses à rejouer).
SKIP_DEFAULT = {"community_shop_seed_guard"}


# --------------------------------------------------------------------------
# Lecture du dump
# --------------------------------------------------------------------------
def locate_dump(path):
    """Retourne (dossier_du_dump, dossier_temporaire_ou_None)."""
    tmp = None
    if os.path.isfile(path):
        tmp = tempfile.mkdtemp(prefix="alicia_dump_")
        with tarfile.open(path) as tar:
            # extraction sûre : refuse les chemins qui sortent du dossier
            base = os.path.realpath(tmp)
            for m in tar.getmembers():
                dest = os.path.realpath(os.path.join(tmp, m.name))
                if not dest.startswith(base + os.sep) and dest != base:
                    raise RuntimeError(f"Chemin suspect dans l'archive : {m.name}")
            tar.extractall(tmp)
        path = tmp
    for root, _dirs, files in os.walk(path):
        if "toc.dat" in files:
            return root, tmp
    raise RuntimeError("toc.dat introuvable : ce n'est pas un dump pg_dump au format directory.")


def read_toc(dump_dir):
    """Retourne {table: (fichier.dat, [colonnes])}."""
    txt = open(os.path.join(dump_dir, "toc.dat"), "rb").read().decode("utf-8", "ignore")
    tables = {}
    for m in re.finditer(r'COPY "public"\."(\w+)" \(([^)]*)\) FROM stdin;', txt):
        name = m.group(1)
        cols = [c.strip().strip('"') for c in m.group(2).split(",")]
        f = re.search(r"(\d+\.dat)", txt[m.end(): m.end() + 300])
        if f:
            tables[name] = (f.group(1), cols)
    return tables


_ESC = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


def unescape(s):
    """Décode le format texte de COPY (\\n, \\t, \\\\, \\NNN, \\xHH...)."""
    if "\\" not in s:
        return s
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        d = s[i + 1]
        if d in _ESC:
            out.append(_ESC[d]); i += 2
        elif d in "01234567":
            j = i + 1
            while j < n and j < i + 4 and s[j] in "01234567":
                j += 1
            out.append(chr(int(s[i + 1:j], 8))); i = j
        elif d == "x" and i + 3 < n + 1 and re.match(r"[0-9a-fA-F]{1,2}", s[i + 2:i + 4]):
            h = re.match(r"[0-9a-fA-F]{1,2}", s[i + 2:i + 4]).group(0)
            out.append(chr(int(h, 16))); i += 2 + len(h)
        else:
            out.append(d); i += 2
    return "".join(out)


def iter_rows(dat_path, ncols):
    """Génère les lignes (liste de str|None) d'un fichier COPY."""
    with open(dat_path, encoding="utf-8", errors="replace", newline="") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.endswith("\r"):
                line = line[:-1]
            if line == "\\.":
                break
            if line == "":
                continue
            parts = line.split("\t")
            if len(parts) != ncols:
                raise ValueError(f"{os.path.basename(dat_path)} : {len(parts)} colonnes au lieu de {ncols}")
            yield [None if p == "\\N" else unescape(p) for p in parts]


# --------------------------------------------------------------------------
# Destination (PostgreSQL ou SQLite)
# --------------------------------------------------------------------------
class Target:
    def __init__(self):
        self.pg = bool(DATABASE_URL)
        if self.pg:
            try:
                import psycopg
            except ImportError:
                sys.exit("psycopg manquant : pip install 'psycopg[binary]'")
            self.con = psycopg.connect(DATABASE_URL)
            self.ph = "%s"
            self.label = "PostgreSQL (DATABASE_URL)"
        else:
            self.con = sqlite3.connect(DB_PATH, timeout=30)
            self.ph = "?"
            self.label = f"SQLite ({DB_PATH})"

    def ensure_schema(self):
        """Laisse bot.py créer ses propres tables, si possible."""
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import bot  # noqa: F401
            bot.init_db()
            print("Tables créées/vérifiées par bot.init_db().")
        except Exception as e:  # dépendances absentes, etc.
            print(f"(init_db non lancé automatiquement : {type(e).__name__}: {e})")

    def columns(self, table):
        """{colonne: type} de la table cible, ou None si elle n'existe pas."""
        if self.pg:
            cur = self.con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s", (table,))
            rows = cur.fetchall()
        else:
            rows = [(r[1], r[2]) for r in self.con.execute(f'PRAGMA table_info("{table}")')]
        return {n: (t or "").lower() for n, t in rows} or None

    def wipe(self, table):
        self.con.execute(f'DELETE FROM "{table}"')

    def insert_many(self, table, cols, rows):
        collist = ", ".join(f'"{c}"' for c in cols)
        marks = ", ".join(self.ph for _ in cols)
        if self.pg:
            sql = f'INSERT INTO "{table}" ({collist}) VALUES ({marks}) ON CONFLICT DO NOTHING'
        else:
            sql = f'INSERT OR IGNORE INTO "{table}" ({collist}) VALUES ({marks})'
        cur = self.con.cursor()
        cur.executemany(sql, rows)

    def count(self, table):
        return self.con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

    def fix_sequence(self, table):
        """PostgreSQL : recale la séquence de la colonne id après import."""
        if not self.pg:
            return
        cur = self.con.execute("SELECT pg_get_serial_sequence(%s, 'id')", (f'public."{table}"',))
        seq = cur.fetchone()[0]
        if seq:
            self.con.execute(
                f'SELECT setval(%s, COALESCE((SELECT MAX(id) FROM "{table}"), 1))', (seq,))

    def commit(self):
        self.con.commit()

    def rollback(self):
        self.con.rollback()


def convert(value, coltype):
    if value is None:
        return None
    t = coltype
    if "int" in t or t in ("serial", "bigserial"):
        return int(value)
    if any(k in t for k in ("double", "real", "numeric", "float", "decimal")):
        return float(value)
    if "bool" in t:
        return value.lower() in ("t", "true", "1")
    return value


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump", help="archive .tar.gz ou dossier du dump")
    ap.add_argument("--dry-run", action="store_true", help="n'écrit rien, affiche seulement le plan")
    ap.add_argument("--wipe", action="store_true", help="vide chaque table cible avant import")
    ap.add_argument("--only", help="liste de tables séparées par des virgules")
    ap.add_argument("--skip", help="tables à ignorer, séparées par des virgules (ex: messages)")
    args = ap.parse_args()

    dump_dir, _tmp = locate_dump(args.dump)
    toc = read_toc(dump_dir)
    only = {x.strip() for x in args.only.split(",")} if args.only else None
    skip = SKIP_DEFAULT | ({x.strip() for x in args.skip.split(",")} if args.skip else set())

    target = Target()
    print(f"Destination : {target.label}")
    print(f"Dump        : {len(toc)} tables\n")
    if not args.dry_run:
        target.ensure_schema()

    total = 0
    for table in sorted(toc):
        fname, dump_cols = toc[table]
        if (only and table not in only) or table in skip:
            continue
        path = os.path.join(dump_dir, fname)
        try:
            raw = list(iter_rows(path, len(dump_cols)))
        except Exception as e:
            print(f"  {table:34s} ERREUR de lecture : {e}")
            continue
        if not raw:
            continue

        tcols = target.columns(table)
        if tcols is None:
            print(f"  {table:34s} {len(raw):>7d} lignes  -> table absente de la base cible, ignorée")
            continue
        keep = [i for i, c in enumerate(dump_cols) if c in tcols]
        dropped = [c for c in dump_cols if c not in tcols]
        cols = [dump_cols[i] for i in keep]
        note = f"  (colonnes ignorées : {', '.join(dropped)})" if dropped else ""

        if args.dry_run:
            print(f"  {table:34s} {len(raw):>7d} lignes  à importer{note}")
            total += len(raw)
            continue

        try:
            rows = [[convert(r[i], tcols[dump_cols[i]]) for i in keep] for r in raw]
            if args.wipe:
                target.wipe(table)
            before = target.count(table)
            for start in range(0, len(rows), 2000):
                target.insert_many(table, cols, rows[start:start + 2000])
            if "id" in cols:
                target.fix_sequence(table)
            target.commit()
            added = target.count(table) - before
            total += added
            print(f"  {table:34s} {len(raw):>7d} lues, {added:>7d} ajoutées{note}")
        except Exception as e:
            target.rollback()
            print(f"  {table:34s} ERREUR : {type(e).__name__}: {e}")

    print(f"\nTerminé. {total} lignes {'à importer' if args.dry_run else 'ajoutées'}.")
    if _tmp:
        import shutil
        shutil.rmtree(_tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
