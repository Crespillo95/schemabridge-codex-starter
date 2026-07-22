# Codex prompts

Each `Mxx_*.md` file is a paste-ready prompt for one fresh Codex chat. The prompt points Codex to the durable repository context and repeats only milestone-specific requirements.

## Use

```bash
python scripts/render_prompt.py M00
```

Then paste the output into Codex. Select the model/reasoning level shown in the milestone file.

## Rules

- Do not concatenate multiple milestone prompts.
- Do not use the master orchestrator prompt to ask for the whole project in one run.
- Use `REPAIR_CURRENT_MILESTONE.md` only after a failed operator test and include the complete failure output.
- Use `REVIEW_CURRENT_DIFF.md` before accepting a high-risk milestone when extra review is useful.
- M16 already defines a bounded Ultra/subagent audit; do not use Ultra by default elsewhere.
