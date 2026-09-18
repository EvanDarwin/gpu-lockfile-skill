---
name: gpu-lockfile
description: Take turns on this machine's shared GPU so multiple agents never benchmark or run models on top of each other. Use it for ANY GPU job that isn't fleeting — a benchmark, a model load or inference/generation run, training, or any timing measurement a co-tenant would ruin. Wrap the command so it waits its turn, runs, and frees the GPU; background it and it resolves on its own.
---

# GPU turn-taking

Several agents share one GPU on this machine. A benchmark or a model run is only trustworthy when it isn't fighting
another job for the card, so **wrap every non-fleeting GPU command** and it will wait its turn instead of
contending.

## When to use it

Wrap the command whenever it will occupy the GPU for more than a moment:

- benchmarks and sweeps
- loading a model and running inference / generation
- training or any fine-tune
- anything whose timing you care about, or that another agent's timing would care about

Skip it only for genuinely instant, throwaway GPU touches (a version probe, a one-token smoke). When unsure,
wrap it — the cost is nil when the board is empty, and you don't risk sullying another agent's job.

## How to use it

One command wraps yours. **Always launch it as a background job.** How long it takes is indeterminate no matter
what you expect: before your command even starts it may wait behind other agents' jobs, and those can run for an
unknown time, so a "quick" job can still sit in line for a while. Don't run it in the foreground and don't assume
it'll return promptly — background it, keep working, and you're notified when it finishes.

```bash
LOCK=~/.claude/skills/gpu-lockfile/scripts/gpu_lock.py

python3 "$LOCK" run "a terse explanation of this job" --eta=600 -- \
  command arg1 arg2 --arg3
```

That reserves a place in line for this one invocation, waits until it's this invocation's turn, runs the command,
and frees the GPU when it exits. The reservation is the run process itself, so it holds the GPU for exactly the
work and not a moment longer.

See the board any time:

```bash
python3 "$LOCK" status
```

## The two things to get right

- **`--eta=SECONDS`**: your honest guess at how long the job runs. It's only a hint — it drives the wait
  estimates other agents see, and it **never** frees your job. A bench that overruns its eta keeps the GPU until
  it's actually done.
- **exclusive vs `--shared`**: exclusive is the default and the right choice for benchmarks and anything you're
  timing — it runs only when the board is clear. Pass `--shared` for GPU work whose numbers don't matter (a
  correctness check, a quick generation); shared jobs run alongside other shared ones but never alongside an
  exclusive one.

## Why it's safe

- **Nothing is freed on a timer.** A slot is held for as long as its process lives and released the instant that
  process exits — cleanly, or by crashing. So a long job is never barged, and a dead job never blocks the GPU.
- **No contention, ever.** Background several wrapped commands and they serialize by arrival order (not always
  the order you'd pick, but they never overlap). Exclusive jobs run alone; shared jobs may double up.
- **Self-healing.** If a job crashes, the next agent to read the board reclaims its slot and moves on.

## Notes

- One invocation = one reservation. For a multi-step bench you want to run back-to-back without another agent
  slipping in between, wrap the whole sequence as a single command — `run "sweep" -- bash -lc 'step1 && step2'` —
  rather than wrapping each step (each step would re-queue independently).
- If a wrapped job is holding the GPU and another agent has a live one queued, that's working as intended: wait,
  or check `status`. Don't launch a bench outside the wrapper to skip the line.
- Cancel a stuck or unwanted reservation with `python3 "$LOCK" release <ticket|pid>` (both are in `status`; any
  fragment of the ticket that names exactly one reservation also works); a run normally releases itself, so this
  is rarely needed.
