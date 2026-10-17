# Entry point for Jarvis's scheduler (job kind "script", args = the budget subcommand):
# Jarvis keeps the clock, Budget does the work. `budget-job.sh usage-refresh` etc.
exec $HOME/.budget/bin/budget "$@"
