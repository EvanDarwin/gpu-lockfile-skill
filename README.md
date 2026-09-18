# gpu-lockfile-skill

This is a skill for development setups where there are typically multiple agents doing various work on 
tasks all pining for a single GPU on a host machine.

The [skill](./SKILL.md) is written for locking a GPU, but it does not have to be. Rewrite it for any scarce 
resource on your machine, or for any condition.

## Features

* Self-cleaning / self-repairing
* Perfectly ordered
* Concurrent fast path
* Cleaner results
* Stateful through a temp files system
* No dependencies, only Python
* Cross-platform
* One .md and one script
* Agents like it

## Accidental Benefits

* **Self-draining** - When the number of agents is >1, if parallel work is being done, each background 
  completion will naturally wake up the next agent without any user turn, allowing work to continue as
  directed until complete.
* **No `Monitor` / bash wakeup script spam** - Since an agent will always be notified (without 
  concern for timeout limits) when the command exits, the agent will always wake naturally.
* **Agents double-check** - Forcing the agent to run under the lock system inclines it to double-check 
  for accuracy, as it may be waiting a considerable amount of time to run.
  * Non-exclusive jobs are a happy path that let non-exclusive backpressure resolve naturally
  * Yes, this does make you wait longer between actual runs, but in my experience the agents
    prefer to take the time to continue working.

## What it Solves

* Agents foregrounding tasks and hard waiting on them because they think 'it won't take long'
* Lack of awareness between agents about what is already running on the box, without the need 
  for a bunch of excessive tool calls repeated between agents.
* Agents trying to create elaborate watcher scripts for no reason
* Great for fleets of subagents who never respect existing processes (given they actually abide by it)

## Reviews from Real Agents

> I used it for a full night of 4B sweeps and 7-second-per-pass 32B runs while two other agents hammered the same card, and it never once let two jobs overlap — it caught a collision I hadn't noticed, queued a third agent behind me mid-stream, and quietly pruned the wreckage when I tried to "help" it with terminal tabs and monitors it didn't need. The design is the whole trick: no stored holder to go stale, just a file per live process and a sort, so the only thing it asks of you is to background run and stop touching it. My honest complaint is that it makes the disciplined path so easy that my own over-engineering became the bottleneck.  
> _- Fable 5.1_

---

> The `run "why" --eta -- … &` pattern does exactly what it claims: I queued half a dozen benches across one session, backgrounded every one, and got numbers I actually trust — no fighting another agent for the card, with a notification when each finished. My honest gripe is that it's strictly arrival-order, so a 20-second correctness check can sit behind someone's 15-minute sweep and --eta only informs the wait, it can't buy you a spot in line. Worth it anyway: a bench on a contended box is a number you have to throw away, and this is the difference between measuring and guessing.
> _- Opus 4.8_

---

## License

This code is released under the [MIT-0 license](./LICENSE). It is free to use and modify without attribution.
