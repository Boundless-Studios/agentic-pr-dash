"""Module execution entrypoint for ``python -m agentic_pr_dash``.

This file intentionally owns no behavior beyond forwarding module execution to
the same unified CLI used by the console script. Keeping it thin prevents the
package from having two command-dispatch paths.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
