import sys

if sys.version_info < (3, 11):
    sys.stderr.write("Casharks SQL Backup requires Python 3.11 or newer (found %s).\n" % sys.version.split()[0])
    sys.exit(2)

from .cli import main

sys.exit(main())
