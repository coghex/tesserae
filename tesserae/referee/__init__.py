"""The AI referee for M1-6 (tracker #63; docs/designs/referee_contract.md).

`store` keeps the referee's conversation, jobs, tool steps and choices in the
game's SQLite file. The Codex runner, the command bridge and the host wiring
arrive in later children (#66 to #69).
"""
