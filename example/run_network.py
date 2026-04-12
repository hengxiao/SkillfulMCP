"""
Compatibility shim — delegates to example.anthropic_sdk.run_network so that
the legacy `python example/run_network.py` and `make example` entry points
keep working. See example/README.md for the full set of framework runners.
"""
from example.anthropic_sdk.run_network import main

if __name__ == "__main__":
    main()
