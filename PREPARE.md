# Preparing the Python Package Template for a New Project

This checklist walks you through converting this template into a clean, correctly named, production-ready Python package. Follow it top to bottom the first time you fork or copy the repository.

---

## 0. Decide Your Names (Do This First)

Before touching files, decide:

* **Project name** (PyPI name, kebab-case)

  * Example: `awesome-tool`
* **Import/package name** (Python module, snake_case)

  * Example: `awesome_tool`
* **Binary / CLI name** (command users will run)

  * Example: `awesome`

Keep these written down — inconsistencies cause subtle breakage later.

---

## 1. Rename the Repository

* Rename the GitHub repository to match the **project name**
* Update any local directory name if you cloned it

```bash
mv python-package-template awesome-tool
```

---

## 2. Rename the Python Package Directory

Find the existing package directory (likely under `src/`).

```bash
src/template_package  ->  src/awesome_tool
```

Then update imports across the codebase:

```bash
grep -R "template_package" -n .
```

---

## 3. Update `pyproject.toml`

Edit the following fields carefully:

### `[project]`

* `name = "awesome-tool"`
* `description = "..."`
* `version = "0.1.0"`
* `authors`
* `readme = "README.md"`
* `license`
* `requires-python`

### Entry Points (CLI)

If the project exposes a binary:

```toml
[project.scripts]
awesome = "awesome_tool.cli:main"
```

Make sure:

* The function exists
* The module path matches the renamed package

---

## 4. Rename the CLI / Binary Internals

* Rename `cli.py` or equivalent if needed
* Update help text, argparse/Typer command names
* Update usage examples in docstrings

Verify locally:

```bash
pip install -e .
awesome --help
```

---

## 5. Update README.md

Replace all template references:

* Project title
* Installation instructions
* CLI usage examples
* Import examples

Checklist:

* ❌ `template-package`
* ❌ `template_package`
* ❌ `template`
* ✅ Your new names only

---

## 6. Update Tests

* Rename test imports
* Update any hardcoded CLI invocations

```bash
pytest
```

Tests should pass **before** publishing anything.

---

## 7. Update Packaging & Metadata Files

Check and update:

* `LICENSE` (correct license and year)
* `MANIFEST.in` (if present)
* `.gitignore` (optional cleanup)
* `CHANGELOG.md` (start fresh if included)

---

## 8. Update Tooling Configuration

Search for template names in:

* `ruff.toml` / `pyproject.toml`
* `mypy.ini`
* `coverage.toml`
* `pre-commit-config.yaml`
* GitHub Actions workflows

Especially:

* Package import paths
* Module names in lint config

---

## 9. Verify Editable Install

From a clean virtual environment:

```bash
pip install -e .
python -c "import awesome_tool; print(awesome_tool.__version__)"
```

If this fails, **do not proceed**.

---

## 10. Final Sanity Check

Run this checklist:

* [ ] `pip install -e .` works
* [ ] CLI command runs
* [ ] Imports resolve correctly
* [ ] Tests pass
* [ ] README examples are accurate
* [ ] No template names remain

```bash
grep -R "template" -n .
```

---

## 11. Prepare for Publishing (Optional)

If publishing to PyPI:

* Ensure version follows semver
* Build locally:

```bash
python -m build
```

* Inspect wheel contents
* Configure trusted publisher or token

---

## Common Mistakes to Avoid

* Mismatch between project name and import name
* Forgetting `[project.scripts]`
* Leaving template names in CI configs
* Publishing before testing editable installs

---

## Recommendation

Treat this checklist as **mandatory** for every new project. Automating parts of it later (cookiecutter, copier, nix flake, etc.) is easier once you understand every step manually.

If you want, I can:

* Turn this into a `PREPARE.md`
* Add a `make rename` helper
* Convert this into a Copier/Cookiecutter flow
