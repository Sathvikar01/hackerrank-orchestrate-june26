import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DATASET_DIR
from pipeline import run_pipeline


def main():
    parser = argparse.ArgumentParser(description="HackerRank Orchestrate evidence review pipeline.")
    parser.add_argument(
        "--input",
        type=str,
        default=str(DATASET_DIR / "claims.csv"),
        help="Path to input claims CSV (default: dataset/claims.csv)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "output.csv"),
        help="Path to output CSV (default: output.csv)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override primary VLM model",
    )
    parser.add_argument(
        "--prompt-version",
        type=str,
        default="v1",
        help="Prompt version to use",
    )
    parser.add_argument(
        "--report",
        type=str,
        default=None,
        help="Optional JSON file to write runtime report",
    )

    args = parser.parse_args()

    print(f"Input: {args.input}")
    print(f"Output: {args.output}")

    stats = run_pipeline(
        input_csv=args.input,
        output_csv=args.output,
        model=args.model,
        prompt_version=args.prompt_version,
    )

    print(f"Processed {stats['rows_processed']} claims, {stats['images_processed']} images.")
    print(f"Total time: {stats['total_time_seconds']:.2f}s")
    print(f"Total tokens: {stats['total_tokens']}")
    print(f"Cache hits: {stats['cache_hits']}")

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
        print(f"Report written to {report_path}")


if __name__ == "__main__":
    main()
