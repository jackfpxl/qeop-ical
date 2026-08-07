"""
TEA NextGen Bot — task dispatcher.

This is the single entry point. It doesn't contain any task logic itself —
it just looks up the requested task in the `tasks/` folder and runs it.

Usage:
    python3 main.py --task export_members

To add a new capability later (e.g. pulling event data from a website):
    1. Create tasks/your_task_name.py
    2. Give it a `run()` function that does the work and writes any
       output files into the `output/` folder
    3. Add "your_task_name" as a new choice in
       .github/workflows/run.yml
That's it — no changes needed here.
"""

import argparse
import importlib
import os
import sys

TASKS_DIR = os.path.join(os.path.dirname(__file__), "tasks")


def available_tasks():
    """Return the list of task names based on files in tasks/."""
    names = []
    for fname in os.listdir(TASKS_DIR):
        if fname.endswith(".py") and not fname.startswith("_"):
            names.append(fname[:-3])
    return sorted(names)


def main():
    parser = argparse.ArgumentParser(description="Run a TEA NextGen Bot task")
    parser.add_argument(
        "--task",
        required=True,
        help=f"Task to run. Available: {', '.join(available_tasks())}",
    )
    args = parser.parse_args()

    if args.task not in available_tasks():
        print(f"ERROR: Unknown task '{args.task}'.")
        print(f"Available tasks: {', '.join(available_tasks())}")
        sys.exit(1)

    os.makedirs("output", exist_ok=True)

    print(f"Running task: {args.task}")
    module = importlib.import_module(f"tasks.{args.task}")
    module.run()
    print(f"Task '{args.task}' finished.")


if __name__ == "__main__":
    main()
