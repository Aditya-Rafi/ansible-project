#!/usr/bin/env python3
import sys
import argparse
import subprocess

def run_query(host, port, user, password, query):
    env = {}
    if password:
        env["MYSQL_PWD"] = password
    cmd = [
        "mysql",
        "-h", host,
        "-P", str(port),
        "-u", user,
        "-B",  # Batch mode, tab-separated
        "-N",  # Skip column headers
        "-e", query
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        print(f"Error running query on {host}:{port}: {query}", file=sys.stderr)
        print(res.stderr, file=sys.stderr)
        sys.exit(res.returncode)
    return res.stdout.strip()

def main():
    parser = argparse.ArgumentParser(description="Verify database integrity 1:1 using Checksums")
    parser.add_argument("--orig-host", required=True)
    parser.add_argument("--orig-port", type=int, default=3306)
    parser.add_argument("--orig-user", required=True)
    parser.add_argument("--orig-password", required=True)
    parser.add_argument("--orig-db", required=True)

    parser.add_argument("--dest-host", required=True)
    parser.add_argument("--dest-port", type=int, default=3306)
    parser.add_argument("--dest-user", required=True)
    parser.add_argument("--dest-password", default="")
    parser.add_argument("--dest-db", required=True)
    args = parser.parse_args()

    # 1. Get original tables (excluding views)
    orig_tables_out = run_query(
        args.orig_host, args.orig_port, args.orig_user, args.orig_password,
        f"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = '{args.orig_db}' AND TABLE_TYPE = 'BASE TABLE'"
    )
    orig_tables = [line.strip() for line in orig_tables_out.splitlines() if line.strip()]

    # 2. Get dest tables (excluding views)
    dest_tables_out = run_query(
        args.dest_host, args.dest_port, args.dest_user, args.dest_password,
        f"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = '{args.dest_db}' AND TABLE_TYPE = 'BASE TABLE'"
    )
    dest_tables = [line.strip() for line in dest_tables_out.splitlines() if line.strip()]

    print(f"======================================================================")
    print(f"Checking Database Integrity: Orig (`{args.orig_db}`) vs Dest (`{args.dest_db}`)")
    print(f"======================================================================")
    print(f"Original Physical Tables: {len(orig_tables)} | Restored Physical Tables: {len(dest_tables)}")

    orig_set = set(orig_tables)
    dest_set = set(dest_tables)

    if orig_set != dest_set:
        missing_in_dest = orig_set - dest_set
        extra_in_dest = dest_set - orig_set
        if missing_in_dest:
            print(f"[-] ERROR: Tables missing in restored database: {missing_in_dest}")
        if extra_in_dest:
            print(f"[-] ERROR: Extra tables in restored database: {extra_in_dest}")
        sys.exit(1)

    print("[+] Structure verification: OK (All physical tables exist in both databases)")
    print("[+] Running 1:1 row count & checksum verification on all physical tables...")

    mismatches = 0
    checked_count = 0

    for tbl in sorted(orig_tables):
        # 1. Compare row count
        cnt_orig_str = run_query(args.orig_host, args.orig_port, args.orig_user, args.orig_password, f"SELECT COUNT(*) FROM `{args.orig_db}`.`{tbl}`")
        cnt_dest_str = run_query(args.dest_host, args.dest_port, args.dest_user, args.dest_password, f"SELECT COUNT(*) FROM `{args.dest_db}`.`{tbl}`")
        cnt_orig = int(cnt_orig_str) if cnt_orig_str else 0
        cnt_dest = int(cnt_dest_str) if cnt_dest_str else 0

        # 2. Compare table checksums
        chk_orig_out = run_query(args.orig_host, args.orig_port, args.orig_user, args.orig_password, f"CHECKSUM TABLE `{args.orig_db}`.`{tbl}`")
        chk_dest_out = run_query(args.dest_host, args.dest_port, args.dest_user, args.dest_password, f"CHECKSUM TABLE `{args.dest_db}`.`{tbl}`")

        chk_orig = chk_orig_out.split('\t')[-1].strip() if chk_orig_out else "NULL"
        chk_dest = chk_dest_out.split('\t')[-1].strip() if chk_dest_out else "NULL"

        if cnt_orig != cnt_dest or chk_orig != chk_dest:
            print(f"[-] MISMATCH on table `{tbl}`:")
            print(f"    Original: Rows={cnt_orig}, Checksum={chk_orig}")
            print(f"    Restored: Rows={cnt_dest}, Checksum={chk_dest}")
            mismatches += 1
        else:
            checked_count += 1

    print(f"======================================================================")
    if mismatches > 0:
        print(f"[-] VERIFICATION FAILED: {mismatches} table(s) did not match 1:1!")
        sys.exit(2)
    else:
        print(f"[+] VERIFICATION SUCCESSFUL: 1:1 match for all {checked_count} physical tables!")
        print(f"    Backup data is 100% healthy, uncorrupted, and matches the original!")
        sys.exit(0)

if __name__ == "__main__":
    main()
