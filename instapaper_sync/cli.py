"""Command line interface for the Instapaper -> Obsidian sync."""

from __future__ import annotations

import argparse
import os
import sys

from . import config as config_module
from .api import Instapaper
from . import audit as audit_module
from . import verify as verify_module
from .sync import Sync


def build_client(args, parsed):
    token = config_module.load_token(parsed, explicit=args.token)
    client = Instapaper(
        token,
        delay=float(parsed.get("request_delay") or 0.3),
        timeout=int(parsed.get("timeout") or 120),
        parse_timeout=int(parsed.get("parse_timeout") or 20),
        log=lambda message: print(f"  {message}", flush=True),
    )
    return client


def cmd_check(args, parsed):
    client = build_client(args, parsed)
    user = client.me()
    premium = "premium" if user.get("premium") else "free"
    print(f"Account: {user.get('username')} (id {user.get('id')}, {premium})")

    total = 0
    for label, kwargs in (
        ("home (to read)", {}),
        ("archive", {"section": "archive"}),
        ("liked", {"section": "liked"}),
    ):
        first = client.list_bookmarks(limit=1, **kwargs)
        count = first.get("total")
        total += count or 0
        print(f"  {label}: {count}")

    folders = client.folders()
    if folders:
        print("  folders:")
        for folder in folders:
            print(f"    - {folder.get('title')} ({folder.get('count')})")
    tags = client.tags()
    print(f"  tags: {len(tags)} ({', '.join(t.get('name', '') for t in tags[:10])}"
          f"{', ...' if len(tags) > 10 else ''})")
    print(f"Total bookmarks across home + archive + liked: {total}")
    return 0


def cmd_sync(args, parsed):
    client = build_client(args, parsed)
    sync = Sync(parsed, client)

    since = None
    if args.since_last:
        since = sync.state.get("last_sync")
        if not since:
            print("No previous sync recorded; running a full sync.")
    sections = args.sections.split(",") if args.sections else None

    if args.recategorize:
        sync.categories.clear()
        sync.llm_categories.clear()

    return sync.run(
        refresh=args.refresh,
        limit=args.limit,
        dry_run=args.dry_run,
        since=since,
        sections=sections,
        workers=args.workers,
        retry_failures=args.retry_failures,
    )


def cmd_audit(args, parsed):
    files, findings = audit_module.scan(
        parsed.vault_path,
        parsed.get("notes_dir") or "Instapaper",
        limit=args.limit,
        index_note=parsed.get("index_note") or "Instapaper Index",
        cache_dir=None if args.no_cache else parsed.get("cache_dir"),
    )
    summary, _with_body = audit_module.summarise(files, findings)
    print(summary)
    print()
    print(audit_module.report(findings, verbose=args.verbose, sample=args.sample))
    errors = sum(1 for severity, _ in findings if severity == "error")
    return 1 if errors and not args.lenient else 0


def cmd_verify(args, parsed):
    findings = verify_module.run(
        parsed.vault_path,
        parsed.get("notes_dir") or "Instapaper",
        index_note=parsed.get("index_note") or "Instapaper Index",
        sample=args.sample,
        seed=args.seed,
        min_ratio=args.min_ratio,
        workers=args.workers,
        timeout=args.timeout,
        log=print,
    )
    print()
    print(verify_module.report(findings, sample=args.sample_size))
    if args.out:
        verify_module.write_report(findings, args.out)
        print(f"\nReport written to {args.out}")
    stats = verify_module.summarise(findings)
    if args.strict and stats["bad"]:
        return 1
    return 0


def cmd_categorize(args, parsed):
    client = build_client(args, parsed)
    sync = Sync(parsed, client)
    counts, methods = sync.categorize_all(
        scope=args.scope, refresh=args.recategorize, limit=args.limit
    )
    total = sum(counts.values()) or 1
    print(f"\n{total} bookmarks")
    for name, count in counts.most_common():
        print(f"  {count:6d}  {100 * count / total:5.1f}%  {name}")
    print("\nby method: " + ", ".join(f"{name} {n}" for name, n in methods.most_common()))
    return 0


def cmd_index(args, parsed):
    client = build_client(args, parsed)
    sync = Sync(parsed, client)
    sync.write_index()
    return 0


def cmd_folders(args, parsed):
    client = build_client(args, parsed)
    for folder in client.folders():
        print(f"{folder.get('id')}\t{folder.get('count')}\t{folder.get('title')}")
    return 0


def cmd_tags(args, parsed):
    client = build_client(args, parsed)
    for tag in sorted(client.tags(), key=lambda t: -(t.get("count") or 0)):
        print(f"{tag.get('count')}\t{tag.get('name')}")
    return 0


def cmd_list(args, parsed):
    client = build_client(args, parsed)
    kwargs = {}
    if args.section:
        kwargs["section"] = args.section
    if args.folder:
        kwargs["folder_id"] = args.folder
    if args.tag:
        kwargs["tag"] = args.tag
    count = 0
    for bookmark in client.iter_bookmarks(page_size=args.limit or 50, **kwargs):
        state = "archived" if bookmark.get("archived") else "to-read"
        print(f"{bookmark.get('id')}\t{state}\t{(bookmark.get('title') or '')[:80]}")
        count += 1
        if args.limit and count >= args.limit:
            break
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="instapaper_sync", description=__doc__)
    parser.add_argument("--config", default="config.json", help="path to config.json")
    parser.add_argument("--token", help="Instapaper API token (overrides config/env/file)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="verify the token and print account stats")
    check.set_defaults(func=cmd_check)

    sync_parser = subparsers.add_parser("sync", help="sync bookmarks into the Obsidian vault")
    sync_parser.add_argument("--refresh", action="store_true", help="re-download article text")
    sync_parser.add_argument("--limit", type=int, help="download at most N articles this run")
    sync_parser.add_argument("--dry-run", action="store_true", help="show what would change")
    sync_parser.add_argument("--since-last", action="store_true", help="only sync changes")
    sync_parser.add_argument("--sections", help="comma separated: home,archive,liked,folders")
    sync_parser.add_argument("--recategorize", action="store_true", help="ignore cached categories")
    sync_parser.add_argument(
        "--workers", type=int, help="parallel article downloads (default: config workers)"
    )
    sync_parser.add_argument(
        "--retry-failures",
        action="store_true",
        help="try again for articles Instapaper previously could not parse",
    )
    sync_parser.set_defaults(func=cmd_sync)

    categorize_parser = subparsers.add_parser(
        "categorize",
        help="recompute categories from cached article text (no note rendering)",
    )
    categorize_parser.add_argument(
        "--recategorize", action="store_true", help="forget cached categories first"
    )
    categorize_parser.add_argument(
        "--scope", choices=("fallback", "all"),
        help="ask the model about everything, or only the unclassified",
    )
    categorize_parser.add_argument("--limit", type=int, help="only the first N bookmarks")
    categorize_parser.set_defaults(func=cmd_categorize)

    index_parser = subparsers.add_parser("index", help="rebuild the index note")
    index_parser.set_defaults(func=cmd_index)

    audit_parser = subparsers.add_parser("audit", help="check the notes already on disk")
    audit_parser.add_argument("--limit", type=int, help="inspect at most N notes")
    audit_parser.add_argument("--sample", type=int, default=10, help="examples per problem")
    audit_parser.add_argument("--verbose", action="store_true", help="print every finding")
    audit_parser.add_argument("--lenient", action="store_true", help="exit 0 even with errors")
    audit_parser.add_argument(
        "--no-cache",
        action="store_true",
        help="skip the completeness check against cached article text",
    )
    audit_parser.set_defaults(func=cmd_audit)

    verify_parser = subparsers.add_parser(
        "verify", help="check stored articles against the live web pages"
    )
    verify_parser.add_argument("--sample", type=int, default=30, help="how many notes to check")
    verify_parser.add_argument("--seed", type=int, default=0, help="sampling seed (reproducible)")
    verify_parser.add_argument("--workers", type=int, default=4, help="pages fetched at once")
    verify_parser.add_argument("--timeout", type=int, default=30, help="seconds per page")
    verify_parser.add_argument(
        "--min-ratio", type=float, default=0.85, help="stored/page word ratio that counts as whole"
    )
    verify_parser.add_argument(
        "--sample-size", type=int, default=10, help="how many bad notes to list (--sample is the sample)"
    )
    verify_parser.add_argument("--out", help="write findings as JSON to this path")
    verify_parser.add_argument("--strict", action="store_true", help="exit 1 if anything is partial")
    verify_parser.set_defaults(func=cmd_verify)

    folders_parser = subparsers.add_parser("folders", help="list Instapaper folders")
    folders_parser.set_defaults(func=cmd_folders)

    tags_parser = subparsers.add_parser("tags", help="list Instapaper tags by usage")
    tags_parser.set_defaults(func=cmd_tags)

    list_parser = subparsers.add_parser("list", help="list bookmarks")
    list_parser.add_argument("--section")
    list_parser.add_argument("--folder", type=int)
    list_parser.add_argument("--tag")
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    try:
        parsed = config_module.load(args.config)
        if args.command != "check":
            parsed.validate()
        return args.func(args, parsed)
    except (RuntimeError, ValueError, FileNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
