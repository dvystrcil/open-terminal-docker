# Container Configuration Test Plan

## Purpose
Validate that the new container environment properly remediates all root causes identified during the previous PR creation workflow.

## Root Causes to Test
| ID | Issue | Fix | Test Method |
|----|-------|-----|-------------|
| **RC1** | Truncated output (pager) | `GIT_PAGER=cat` + `~/.gitconfig` | Check git command output completeness |
| **RC2** | Missing environment variables | `~/.bashrc` exports | Verify env vars load automatically |
| **RC3** | Git authentication issues | Remote URL with token | Verify git push works without prompting |
| **RC4** | Manual step errors | `create-pr.sh` script | Run full automated workflow |

---

## Test Cases

### TC1: Verify Environment Variables Load Automatically

**Purpose:** Confirm `~/.bashrc` loads environment variables in new container session.

**Steps:**
```bash
# Check Git pager is set
echo $GIT_PAGER

# Check LESS is set
echo $LESS

# Check GH_TOKEN is set (may be empty if not configured)
echo $GH_TOKEN
```

**Expected Output:**
```
GIT_PAGER: cat
LESS: -RXF
GH_TOKEN: <token or empty>
```

**Pass Criteria:** `GIT_PAGER=cat` and `LESS=-RXF` are set


### TC2: Verify Git Configuration

**Purpose:** Confirm `~/.gitconfig` settings are loaded.

**Steps:**
```bash
git config --global --list
```

**Expected Output:**
```
core.pager=cat
init.defaultbranch=main
```

**Pass Criteria:** No pager (`less`) configured, `cat` is used


### TC3: Verify Git Commands Don't Use Pager

**Purpose:** Confirm git commands output fully without truncation.

**Steps:**
```bash
# Run git log (should show all lines without pagination)
git log --oneline -20

# Check git status output
git status
```

**Expected Output:**
- All output displayed (no `--more--` prompt)
- No "quit by pressing q" message

**Pass Criteria:** Output complete without pagination


### TC4: Verify Helper Functions Available

**Purpose:** Confirm helper functions from `~/.bashrc` are loaded.

**Steps:**
```bash
# Test function existence
type verify_pr
type verify_push
type setup_git_auth
```

**Expected Output:**
```
verify_pr is a function
verify_push is a function
setup_git_auth is a function
```

**Pass Criteria:** All three functions are defined


### TC5: Verify Helper Script Exists and is Executable

**Purpose:** Confirm `create-pr.sh` is available.

**Steps:**
```bash
ls -la ~/create-pr.sh
head -5 ~/create-pr.sh
```

**Expected Output:**
```
-rwxr-xr-x ... create-pr.sh
#!/bin/bash
set -e
```

**Pass Criteria:** File exists with executable permissions (`-rwx`)


### TC6: Verify GitHub CLI Authentication

**Purpose:** Confirm `gh` CLI is authenticated.

**Steps:**
```bash
gh auth status
```

**Expected Output:**
```
github.com
  ✓ Logged in to github.com
  ✓ Token: ghp_xxx
  ✓ Git operations: ghp_xxx
```

**Pass Criteria:** Shows "Logged in" status


### TC7: Test Git Remote Authentication

**Purpose:** Verify git can push without username/password prompt.

**Steps:**
```bash
# Check current remote URL
git remote get-url origin

# Test fetch (should not prompt for credentials)
git fetch origin 2>&1 | head -5
```

**Expected Output:**
```
Remote URL contains: x-access-token:ghp_xxx@github.com
Fetch completes without credential prompt
```

**Pass Criteria:** No "could not read Username" error


### TC8: Test Full PR Workflow (End-to-End)

**Purpose:** Validate complete automated workflow works.

**Steps:**
```bash
# Create test branch
./create-pr.sh "test-$(date +%s)" "Test commit" "Test PR Title" "Test body"
```

**Expected Output:**
- All steps complete without manual intervention
- PR created successfully
- Output shows `✓` markers for each step

**Pass Criteria:** Script completes with `✅ PR creation complete!`


### TC9: Test Command Output Completeness

**Purpose:** Verify no truncation in command outputs.

**Steps:**
```bash
# Run command with potentially large output
git log --all --oneline -100 > /tmp/git_log.txt
wc -l /tmp/git_log.txt
cat /tmp/git_log.txt | tail -10
```

**Expected Output:**
- All lines captured (no `... [Content collapsed] ...`)
- File contains expected number of lines

**Pass Criteria:** Output file contains complete data


### TC10: Verify Documentation Files

**Purpose:** Confirm setup documentation is available.

**Steps:**
```bash
ls -la ~/SETUP_INSTRUCTIONS.md
ls -la ~/.git_workflow_setup.md
```

**Expected Output:**
```
-rw-r--r-- ... SETUP_INSTRUCTIONS.md
-rw-r--r-- ... .git_workflow_setup.md
```

**Pass Criteria:** Both files exist


---

## Execution Checklist

```
[ ] TC1: Environment variables load
[ ] TC2: Git configuration loaded
[ ] TC3: No pager truncation
[ ] TC4: Helper functions available
[ ] TC5: Helper script executable
[ ] TC6: GitHub CLI authenticated
[ ] TC7: Git remote authenticated
[ ] TC8: Full PR workflow works
[ ] TC9: Command output complete
[ ] TC10: Documentation available
```

---

## Quick Validation Command

Run this single command to verify most configurations:

```bash
echo "=== CONTAINER CONFIG VALIDATION ===" && \
echo "1. GIT_PAGER: $GIT_PAGER" && \
echo "2. LESS: $LESS" && \
echo "3. Git config:" && git config --global --list && \
echo "4. Helper script:" && ls -lh ~/create-pr.sh && \
echo "5. GitHub auth:" && gh auth status 2>&1 | head -3 && \
echo "=== VALIDATION COMPLETE ==="
```

---

## Failure Analysis

If any test fails:

| Test | Likely Cause | Fix |
|------|--------------|-----|
| TC1 | `~/.bashrc` not sourced | Run `source ~/.bashrc` |
| TC2 | Git config not loaded | Check `~/.gitconfig` exists |
| TC3 | Pager still active | Verify `core.pager=cat` in git config |
| TC4 | Functions not loaded | Re-source `~/.bashrc` |
| TC5 | Script not executable | Run `chmod +x ~/create-pr.sh` |
| TC6 | GitHub CLI not auth'd | Run `gh auth login` |
| TC7 | Remote URL missing token | Run `setup_git_auth` function |
| TC8 | Script errors | Check script permissions and env vars |

---

## Pass Criteria Summary

**Container passes validation if:**
- ✅ All 10 test cases pass
- ✅ No `... [Content collapsed] ...` in outputs
- ✅ No credential prompts from git
- ✅ `create-pr.sh` completes successfully
- ✅ All helper functions are defined

**Expected Result:** All root causes (RC1-RC4) are remediated.

---

## Status

**Status:** ⏳ Awaiting container deployment

**Next Steps:**
1. Deploy new container with updated Dockerfile/entrypoint
2. Open terminal session in container
3. Run `echo "=== CONTAINER CONFIG VALIDATION ===" && ...` (Quick Validation Command above)
4. Report results with any failures
