# Distribution checks

The V1 release was built from a scratch copy using Python 3.12 and isolated PEP 517 build environments. The build produced a source distribution and rebuilt the pure-Python wheel from that archive. A fresh virtual environment installed the wheel with its PostgreSQL extra and resolved the declared dependencies from PyPI. `pip check` passed.

From a directory outside the checkout, `python -I -m investment_intelligence demo` created the four Markdown briefs, a SQLite database and versioned evidence with zero warnings. The installed `investment-intelligence doctor` entry point reported each connector and missing optional credentials on a separate empty live database. The wheel includes the Agent Skill under the environment's `share/investment-intelligence/skills` directory.

The packaging regression places random sentinel bytes in nine synthetic private paths, builds both archives and checks every regular file. The official Docker Python SDK also assembled a build-context tarball from that seeded checkout. It included the application, its public configuration, fixtures and skill and excluded all nine private files. The Compose definition passed the official Compose JSON schema. Workflow dispatch choices matched the CLI, both workflows restrict repository permissions to read, and shell steps passed `bash -n`. Docker and hosted Actions execution were unavailable on the verification host.

Run the packaging and deployment regression tests after installing the test extra:

```sh
python -m pytest -q tests/test_packaging.py tests/test_deployment.py
```
