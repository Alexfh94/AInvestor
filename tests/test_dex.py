"""Tests for DEX placeholder."""

import pytest

from ainvestor.dex import DexConnector


@pytest.mark.asyncio
async def test_dex_not_enabled_by_default():
    dex = DexConnector()
    assert dex.is_enabled is False
    gaps = await dex.detect_cex_gaps(["BTC/USDT"])
    assert gaps == []
