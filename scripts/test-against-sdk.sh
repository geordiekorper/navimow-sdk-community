#!/bin/sh
# A consumer made for the Consumers check workflow: it installs nothing,
# prints the version it was given, if any, and fails with status 3.
echo "test-against-sdk.sh: given $# argument(s): $*"
exit 3
