import sys

from r2_batch_download.update_install import CLEANUP_ARGUMENT, UPDATE_ARGUMENT, run_update_helper

if __name__ == "__main__":
    if sys.argv[1:2] == [UPDATE_ARGUMENT]:
        raise SystemExit(run_update_helper(sys.argv[2:]))

    from r2_batch_download.client import main

    workspace = ""
    if len(sys.argv) == 3 and sys.argv[1] == CLEANUP_ARGUMENT:
        workspace = sys.argv.pop()
        sys.argv.pop()
    main(update_workspace=workspace)
