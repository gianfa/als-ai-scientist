import argparse

from .runner import run_analysis, run_gwas_analysis
from .verify import verify_receipt, verify_run


def main():
    parser = argparse.ArgumentParser(description="Run or verify deterministic ALS analyses")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--analysis", required=True)
    run.add_argument("--source-root", default="dist/michaelis-2025-agent/skill/michaelis-2025-paper")
    run.add_argument("--output-root", required=True)
    run.add_argument("--gwas-file", help="required for a3_gwas_loci")
    run.add_argument("--seed", type=int, default=20260928)
    run.add_argument("--run-id")
    run.add_argument("--config-hash")
    verify = sub.add_parser("verify")
    verify.add_argument("summary")
    verify.add_argument("--receipt")
    verify_receipt_cmd = sub.add_parser("verify-receipt")
    verify_receipt_cmd.add_argument("receipt")
    args = parser.parse_args()
    if args.command == "run":
        if args.analysis == "a3_gwas_loci":
            if not args.gwas_file:
                parser.error("a3_gwas_loci requires --gwas-file")
            run_gwas_analysis(args.gwas_file, args.output_root, args.seed, args.run_id, args.config_hash)
        else:
            run_analysis(args.analysis, args.source_root, args.output_root, args.seed, args.run_id, args.config_hash)
    elif args.command == "verify":
        verify_run(args.summary, args.receipt)
        print("verified")
    else:
        verify_receipt(args.receipt)
        print("verified")


if __name__ == "__main__":
    main()
