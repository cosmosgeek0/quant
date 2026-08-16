"""Shared Binance hand (frozen interface: study/design/EXECUTION-HAND-INTERFACE.txt).

Adapters: binance_http (signer, one-attempt transport, bounded reads, shared
types), binance_spot, binance_futures, binance_margin, binance_transfer.
Adapters import types only from binance_http and never from one another.
"""
