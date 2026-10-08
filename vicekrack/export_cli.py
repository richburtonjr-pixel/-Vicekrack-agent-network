"""Step 38 commands: portable, locally viewable content-preview packages (never uploads or publishes).

export-preview PRODUCTION_ID --report REPORT_ID --purpose review_copy|approved_preview
               [--include-reviewer-labels] [--include-review-notes]
                    copy an allowlisted set of files into a new package under runtime/exports/.
export-verify PATH  check a package anywhere, without the production (read-only). Exit 0 only when
                    consistent. Hashes are consistency checks, not signatures.
"""
import argparse
import json

from .errors import NetworkError
from .export import PURPOSES, PreviewExporter, verify_package


def main():
    parser = argparse.ArgumentParser(description="Portable local preview packages; never uploads or publishes")
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("export-preview")
    make.add_argument("production_id", metavar="PRODUCTION_ID")
    make.add_argument("--report", required=True, metavar="REPORT_ID", help="a Step 36 bound quality report")
    make.add_argument("--purpose", required=True, choices=PURPOSES)
    make.add_argument("--include-reviewer-labels", action="store_true",
                      help="include self-declared reviewer labels (left out by default)")
    make.add_argument("--include-review-notes", action="store_true",
                      help="include review notes, which may contain sensitive text (left out by default)")
    check = commands.add_parser("export-verify")
    check.add_argument("path", metavar="PATH")
    args = parser.parse_args()
    try:
        if args.command == "export-verify":
            result = verify_package(args.path)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["status"] == "consistent" else 1
        manifest, target = PreviewExporter().export(
            args.production_id, args.report, purpose=args.purpose,
            include_reviewer_labels=args.include_reviewer_labels, include_review_notes=args.include_review_notes)
        print(json.dumps({"package_dir": str(target), "package_id": manifest["package_id"],
                          "purpose": manifest["purpose"], "exported_at": manifest["exported_at"],
                          "files": len(manifest["files"]) + 1, "restrictions": manifest["restrictions"],
                          "privacy": manifest["privacy"], "note": manifest["integrity"]["note"]}, indent=2))
        return 0
    except NetworkError as error:
        print(json.dumps({"error": error.as_dict()}))
    except (OSError, ValueError, UnicodeError):
        print('{"error": {"code": "invalid_input_or_storage"}}')
    return 1
