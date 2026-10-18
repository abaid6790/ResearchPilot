"""Research agents and the LangGraph workflow.

Agents are plain functions that call the LLM router with a fixed JSON schema and write their
results to the database. They have no tools of their own: the only things that touch the network
are the search client and the safe fetcher, called by ordinary code, never by a model.
"""
