#!/bin/bash
# Automated PR creation script with verification at each step
set -e  # Exit on any error

# Check if we have the right number of arguments
if [ $# -lt 4 ]; then
    echo "Usage: $0 <branch-name> <commit-message> <pr-title> <pr-body>"
    echo "Example: $0 redis-deployment 'Add Redis 7.2' 'Add Redis 7.2 Deployment' 'Details...'"
    exit 1
fi

BRANCH=$1
COMMIT_MSG=$2
PR_TITLE=$3
PR_BODY=$4

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}🔄 Starting PR creation process...${NC}"

# Step 1: Create and checkout branch
echo -e "${BLUE}1/5: Creating branch '${BRANCH}'...${NC}"
git checkout -b "$BRANCH" || git checkout "$BRANCH"
echo -e "${GREEN}✓ Branch created/switched${NC}"

# Step 2: Add all changes
echo -e "${BLUE}2/5: Staging changes...${NC}"
git add -A
git status -s
if [ -z "$(git status -s)" ]; then
    echo "No changes to commit!"
    exit 0
fi
echo -e "${GREEN}✓ Files staged${NC}"

# Step 3: Commit
echo -e "${BLUE}3/5: Committing changes...${NC}"
git commit -m "$COMMIT_MSG"
echo -e "${GREEN}✓ Committed${NC}"

# Step 4: Push to remote
echo -e "${BLUE}4/5: Pushing to remote...${NC}"
git push -u origin "$BRANCH"
echo -e "${GREEN}✓ Pushed${NC}"

# Step 5: Create PR
echo -e "${BLUE}5/5: Creating Pull Request...${NC}"
gh pr create --title "$PR_TITLE" --body "$PR_BODY"

# Verification
echo -e "${BLUE}🔍 Verification...${NC}"
echo "Checking open PRs..."
gh pr list --state open

echo -e "${GREEN}✅ PR creation complete!${NC}"
echo "PR link should be available in the output above."