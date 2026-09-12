"""Desktop entry point and reproducible command-line analysis."""
from __future__ import annotations
import argparse
import json
import sys

def main(argv=None):
    parser = argparse.ArgumentParser(description="EMI Assistant for KiCad")
    parser.add_argument("--board", help="Open a .kicad_pcb file")
    parser.add_argument("--demo", action="store_true", help="Try the included example")
    parser.add_argument("--connect", action="store_true", help="Analyze the open KiCad board")
    parser.add_argument("--analyze", action="store_true", help="Run without the desktop interface")
    parser.add_argument("--output", help="Save a JSON or HTML report")
    parser.add_argument("--layout-only", action="store_true", help="Skip simulations for this command-line analysis")
    args = parser.parse_args(argv)
    from .controller import Controller
    controller = Controller()
    try:
        if args.demo:
            controller.load_demo()
        elif args.board:
            controller.open_board(args.board)
        if args.analyze:
            result = controller.result or controller.analyze()
            if not args.layout_only:
                controller.run_simulations(progress=lambda message: print(message, file=sys.stderr))
            if args.output:
                controller.export_report(args.output)
                print(f"Saved {args.output}")
            else:
                print(json.dumps(result.to_dict(), indent=2, allow_nan=False))
            return 0
        from .ui import run
        return run(controller, auto_connect=args.connect)
    except (ImportError, RuntimeError, ValueError, OSError) as exc:
        print(f"EMI Assistant: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    sys.exit(main())
