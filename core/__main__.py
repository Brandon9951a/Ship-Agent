from core.pipeline import run_placeholder_pipeline


def main() -> None:
    print("agent_v2 placeholder pipeline started")
    for result in run_placeholder_pipeline():
        print(f"[{result.status}] {result.name}: {result.detail}")
    print("agent_v2 placeholder pipeline completed")


if __name__ == "__main__":
    main()
