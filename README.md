# MergePreflight

MergePreflight rehearses merging two or more branches in a disposable Git worktree. It reports conflicts, changed-path overlap, selected interface risks, and results from checks you explicitly configure. Your source branches and primary worktree remain untouched.

## Install

Requires Python 3.11 or newer and Git. From this repository:

```sh
python3 -m pip install -e .
```

## Use

```sh
merge-preflight check --repo /path/to/repo --base main --branches feature/a feature/b \
  --test 'pytest -q' --format json --out preflight.json
merge-preflight explain preflight.json
```

`--test` checks are required. For advisory checks, pass `--config checks.json` with entries such as `{"checks":[{"command":"pytest -q","required":false}]}`. Use `--log-dir /path/outside/repo` to retain full check output; reports keep bounded excerpts and hashes. `merge-preflight cleanup --repo /path/to/repo` lists retained tool worktrees, and `--yes` removes them.

Run the tests with `python3 -m unittest discover -s tests -q`.
