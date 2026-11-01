"""Bounded, inspectable Magic question workflow built with LangGraph.

Start with graph.QuestionWorkflow for Python integration or cli.main for the
magic-chat command. state defines data contracts, nodes implements each stage,
references handles evidence expansion, memory owns SQLite/turn locks, and context
selects recent messages without deleting stored history.
"""
