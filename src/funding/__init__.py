"""Money ingesters. Every one of them produces CLAIMS, not facts.

A source here does not get a table of its own (AD-100). It writes rows
into `cost_claims` carrying its own name in `source`, and the resolver
views decide what to show. Two sources disagreeing is the product, not a
bug to reconcile away.
"""
