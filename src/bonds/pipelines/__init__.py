"""Pipelines — orchestration that wires sources to storage, one module per pillar."""

from bonds.pipelines.base import PipelineResult, RunStatus
from bonds.pipelines.bond_report import NseBondReportPipeline
from bonds.pipelines.corporate_trade import (
    BseCorporateTradePipeline,
    NseCorporateTradePipeline,
)
from bonds.pipelines.liquidity import LiquidityPipeline
from bonds.pipelines.public_issue import PublicIssuePipeline
from bonds.pipelines.rbi_auction import RbiAuctionPipeline
from bonds.pipelines.rbi_auction_result import RbiAuctionResultPipeline
from bonds.pipelines.sovereign_valuation import SovereignValuationPipeline
from bonds.pipelines.spread_matrix import SpreadMatrixPipeline
from bonds.pipelines.trade import TradePipeline
from bonds.pipelines.universe import UniversePipeline
from bonds.pipelines.yield_curve import YieldCurvePipeline

__all__ = [
    "BseCorporateTradePipeline",
    "LiquidityPipeline",
    "NseBondReportPipeline",
    "NseCorporateTradePipeline",
    "PipelineResult",
    "PublicIssuePipeline",
    "RbiAuctionPipeline",
    "RbiAuctionResultPipeline",
    "RunStatus",
    "SovereignValuationPipeline",
    "SpreadMatrixPipeline",
    "TradePipeline",
    "UniversePipeline",
    "YieldCurvePipeline",
]
