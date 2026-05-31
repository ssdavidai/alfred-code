---
description: "Cut a release: CHANGELOG narrative + annotated tag + GH Release"
allowed-tools: Bash(git:*, gh:*, date:*), Read, Edit, Write
argument-hint: "[version-tag, defaults to v$(date +%Y.%m.%d)]"
---

# Previous tag + commits since

!`git tag --sort=-creatordate | head -3`

!`prev=$(git tag --sort=-creatordate | head -1); echo "Commits since $prev:"; git --no-pager log --oneline "$prev"..HEAD | head -40`

# PRs merged since previous tag

!`prev=$(git tag --sort=-creatordate | head -1); since=$(git log -1 --format=%cI "$prev"); gh pr list --state merged --search "merged:>$since" --json number,title,mergedAt,labels --jq '.[] | "  #\(.number)  [\((.labels // []) | map(.name) | join(\",\"))]  \(.title[:90])"' | head -40`

---

You are the release author. Write a **two-paragraph principal-readable narrative** for `CHANGELOG.md` under a new heading `## [YYYY-MM-DD]`.

## Style

Match the most recent CHANGELOG section's voice. **Read the top entry of CHANGELOG.md** before writing — that's the style template. Sir's narrative voice:
- Opens with "Alfred Black becomes **X**." or similar one-sentence framing of what the principal-visible change is
- Then explains what was true before and what's true after
- Names the actual mechanism in one or two clear sentences (not jargon)
- Lists PRs by number with one-line summaries grouped by theme

## Then

1. Edit `CHANGELOG.md` — insert the new entry at the top under the title, above the previous entry.
2. Commit:
   ```
   git add CHANGELOG.md
   git commit -m "docs(release): YYYY-MM-DD — <one-line summary>" \
     -m "" \
     -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
   ```
3. Push to current branch.
4. Tag annotated with the same narrative:
   ```
   tag=${1:-v$(date +%Y.%m.%d)}
   git tag -a "$tag" -m "<copy of the narrative>"
   git push origin "$tag"
   ```
5. Create the GH Release with the same narrative as the body:
   ```
   gh release create "$tag" --title "$tag — <summary>" --notes-file <(cat <<EOF
   <narrative>
   EOF
   )
   ```

## Honesty rule

If the release narrative would have to overstate what shipped (e.g. "X is now live" when X smoke partial-shipped) — **state the partial honestly** in the narrative. The CHANGELOG is the record; the record is durable; integrity beats marketing.
