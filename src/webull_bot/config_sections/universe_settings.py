from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class UniverseSettings(BaseSettings):
    """Symbol universe resolution, scan batching, and curated candidate lists."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    stock_symbols: str = "ALL"
    option_contracts: str = ""
    option_underlyings: str = ""
    option_type: str = Field(default="BOTH", pattern="^(CALL|PUT|BOTH)$")
    # By request: "still at least 2 weeks out" - contracts nearer to
    # expiration than this are never considered at entry (see
    # WebullAPI.select_atm_options), regardless of how good the strike/
    # delta otherwise looks.
    option_min_dte: int = Field(default=14, ge=0, le=730)
    option_max_dte: int = Field(default=45, ge=0, le=730)
    max_symbols: int = Field(default=800, ge=0, le=50000)
    stock_universe_reserve: int = Field(default=400, ge=0, le=50000)
    stock_universe_page_size: int = Field(default=200, ge=25, le=1000)
    # By request, after live evidence: at a large MAX_SYMBOLS (today's
    # live value: 5000), the once-daily universe download + VOLFILT
    # historical-volatility scoring of the WHOLE universe took 15-20
    # minutes before AutoTrader.stock_symbols was populated at all -
    # position protection was already fixed to never block on this
    # (see resolve_targets), but no NEW entry could fire the entire
    # time either, since trade_stocks had nothing to scan. Splits the
    # once-daily load into a small, fast initial batch (unblocks
    # trading in roughly a minute instead of 15-20) followed by
    # continued growth toward the full MAX_SYMBOLS in the background -
    # see AutoTrader._grow_stock_universe. Only applies when
    # STOCK_SYMBOLS=ALL; an explicit symbol list is already small and
    # fast to resolve.
    stock_universe_initial_limit: int = Field(default=500, ge=1, le=50000)
    stock_universe_growth_batch_size: int = Field(default=1000, ge=1, le=50000)
    stock_universe_growth_interval_seconds: int = Field(
        default=180, ge=30, le=3600
    )
    stock_batch_size: int = Field(default=100, ge=1, le=300)
    # By request: "scan through all [the universe], split it up in
    # parallel streams... as many as needed to scan everything and
    # filter it down, then dynamically less as it is filtered down...
    # does not need to be as intense in extended hours." A single
    # trade_stocks cycle previously fetched exactly one STOCK_BATCH_SIZE
    # (100, Webull's own hard per-call snapshot cap) worth of quotes -
    # at a large, still-growing universe (up to MAX_SYMBOLS), that's a
    # small fraction of the universe covered per cycle. Fires multiple
    # STOCK_SNAPSHOT_MAX_SYMBOLS-sized quote batches CONCURRENTLY
    # instead (see AutoTrader.stock_scan_concurrent_batches/trade_
    # stocks) - the batch count scales with how large the current
    # universe is (more concurrent batches while it's still large/
    # freshly grown, dynamically fewer as prioritized_stock_batch's own
    # activity-based ranking naturally concentrates real candidates),
    # bounded by stock_scan_max_concurrent_batches as a hard safety cap
    # on real request volume (Webull's API already returned live 429
    # TOO_MANY_REQUESTS errors this session - see _is_rate_limited).
    # By request, after live evidence: at the original default (20),
    # concurrent batching only kicks in past batch_size*20=2000
    # symbols - with a live universe still growing toward MAX_SYMBOLS
    # (5000) and sitting at ~1846 for a long stretch along the way,
    # this meant scanning stayed at a single 100-symbol batch per
    # cycle the whole time, not the "more candidates, more trades more
    # frequently" a genuinely concurrent multi-batch scan should
    # deliver. Lowered to 5 - concurrency now starts mattering at
    # batch_size*5=500 symbols (already active almost immediately
    # after the fast initial universe load) and scales up faster as
    # the universe grows, while stock_scan_max_concurrent_batches (8)
    # and the 429-retry logic (_retry_once_on_rate_limit) still bound
    # the real request-volume risk.
    stock_scan_target_full_coverage_cycles: int = Field(
        default=5, ge=1, le=500
    )
    # By request: "we want entry and profit to be quicker" - live
    # evidence showed a full trade_stocks cycle (needed to detect a
    # FRESH entry signal, or a held position's FIRST crossing into
    # profit/loss territory before any exit order exists to actively
    # manage) only completing every 30-90+ seconds, well past the point
    # this cap (8) becomes the binding constraint on a large universe -
    # at stock_batch_size=100, needed concurrent batches already
    # exceeds 8 whenever the universe is above ~4000 symbols with the
    # default 5-cycle coverage target, so this cap was the actual
    # throughput ceiling for most of the day, not the coverage-cycles
    # target above it. Raised 8 -> 12 (a 50% increase in real request
    # volume) - still well short of the field's own max (50), and
    # _retry_once_on_rate_limit already backstops the live 429
    # TOO_MANY_REQUESTS risk this was originally capped against.
    stock_scan_max_concurrent_batches: int = Field(default=12, ge=1, le=50)
    stock_scan_extended_hours_concurrency_fraction: Decimal = Field(
        default=Decimal("0.5"), ge=0, le=1
    )
    stock_priority_fraction: float = Field(default=0.70, ge=0, le=0.90)
    stock_penny_fraction: float = Field(default=0.10, ge=0, le=0.50)
    penny_stock_max_price: Decimal = Field(default=Decimal("5"), gt=0)
    exclude_etfs: bool = True
    popular_stock_symbols: str = (
        "NVDA,TSLA,AMD,AAPL,AMZN,META,MSFT,GOOGL,NFLX,AVGO,"
        "COIN,PLTR,MSTR,HOOD,SOFI,RIVN,GME,AMC,NIO,BABA,F,SNAP,UBER,"
        "MARA,IONQ,RGTI,QBTS,QUBT,FCX"
    )
    # Always present in the dashboard watchlist from the moment the bot
    # starts, every run - unlike POPULAR_STOCK_SYMBOLS (which only weights
    # priority within the scanned universe), these are seeded directly into
    # user_watchlist so a restart never loses them and the user never has to
    # re-add them from the dashboard.
    default_watchlist_symbols: str = (
        "AAPL,F,NVDA,MSFT,AMZN,NFLX,TSLA,NIO,ADBE,XPEV,OXY,NOW,XOM,DDOG,OPTT,"
        "FCEL,CVX,NET,FEMY,CRM,CHPT,KO,AMC,ABNB,BNKK,SNAP,DASH,SBUX,HWH,LCID,"
        "SIRI,BNGO,NVO,SPCX,SPCE,SHOP,GM,RIVN,IBM,ZM,NEGG,NVGS,ZVRA,V,MRNA,T,"
        "PYPL,PPSI,DIS,JNJ,COST,ROKU,SPYD,SPY,UBER,COIN,XYZ,WMT,DFLI,PLTR,PFE,"
        "UNH,TBB,VZ,BABA,SRPT,MARA,AVGO,GOOGL,GOOG,BB,FDX,BRK-A,GNW,OPAD,SPX,"
        "ACB,ORCL,IXIC,META,QQQ,RBLX,UPS,QCOM,AAL,CCX,SKYA,CRWD,CTRM,NKE,OCGN,"
        "SNDA,WWR,INTC,HD,NIU,GME,DIA,ATOS,BAD,DKNG,UAL,MOBX,HOOD,DAL,CLOV,"
        "RKLB,TSM,MU,JPM,AMD,NOK,BA,RIOT,TLRY,SOFI,CENN"
    )
    popular_stock_min_volume: int = Field(default=1_000_000, ge=0)
    # By request: "we want options for more popular stocks only like
    # in snp and dow" / "make sure the stocks selected for options are
    # popular like snp500." A curated, large-cap-heavy set spanning
    # major sectors (tech, financials, healthcare, consumer, energy,
    # industrials, communications) - deliberately NOT a literal,
    # complete enumeration of current S&P 500 membership (which
    # changes over time and would risk a wrong/delisted ticker slipping
    # in unverified); every symbol here is a well-known, currently-
    # listed large/mega-cap name. Replaces self.stock_symbols (which
    # can be the ENTIRE scanned universe, thousands of symbols in
    # STOCK_SYMBOLS=ALL mode, including penny/micro-cap names) as
    # discover_option_contracts' candidate pool - see its own comment.
    # .env can override/extend this.
    # By explicit request, after a live session locked a cohort of ONE:
    # "we need all popular, voluminous, and volatile stocks, more than
    # what is there, but not too many to not be able to monitor."
    #
    # The original list was ~123 pure mega-caps, and it was the single
    # binding constraint on focus mode. Two independent failures,
    # measured live on 2026-09-22:
    #   1. Mega-caps structurally do not gap. At 08:34 CT not ONE name
    #      here cleared the 2% batch gate - best was ADBE at 1.92% on
    #      278k shares (failing the 1M volume floor), then GOOGL 1.57%,
    #      AAPL 0.91%, NVDA 0.70%. The cohort locked with one name.
    #   2. Mega-cap CONTRACTS are unaffordable on a small account. A
    #      $900 COST or $1000 LLY contract sizes to zero against $363
    #      no matter how well it sets up, so half this list could never
    #      have been traded even on a perfect signal.
    # Meanwhile SHOP, UAL, DASH, COIN, PLTR, RIVN and friends - all
    # deeply liquid with real, cheap option chains - were excluded
    # entirely, which is why the pool kept coming back as 7-11 names.
    #
    # This is NOT a relaxation of the GRML/GRAL lesson ("the stocks we
    # pick should be like fortune 500, or snp, or dow... popular,
    # known, established"). GRML was a 286% gapper with NO listed
    # options at all. Every name added below is a widely held,
    # currently listed company with a deep, actively quoted options
    # market - the quality bar is unchanged, the universe is simply no
    # longer restricted to the most expensive and least volatile
    # corner of it. Deliberately dropped in exchange: the staples and
    # utilities (DUK, SO, KO, PG, CL, MO, PM, WM, LIN, APD...) that
    # cannot produce a momentum setup and only slowed the discovery
    # rotation. Net ~200 names - roughly the monitoring budget asked
    # for, and every one of them can actually move.
    option_candidate_symbols: str = (
        # Mega-cap core (kept: still the most liquid chains on the tape)
        "AAPL,MSFT,GOOGL,GOOG,AMZN,META,NVDA,AVGO,TSLA,BRK-B,LLY,V,"
        "UNH,JPM,XOM,MA,COST,HD,NFLX,JNJ,ABBV,BAC,CRM,MRK,AMD,PEP,"
        "TMO,CSCO,WMT,ACN,ADBE,MCD,ORCL,DHR,WFC,TXN,NOW,IBM,GE,CAT,"
        "INTU,VZ,DIS,AMGN,QCOM,CMCSA,PFE,UNP,LOW,UBER,AMAT,HON,ISRG,"
        "BKNG,GS,MS,BLK,T,SCHW,DE,LMT,C,VRTX,TJX,SBUX,REGN,BSX,GILD,"
        "PANW,BA,MU,ADI,BX,KLAC,EOG,SNPS,CDNS,NKE,PYPL,CSX,TGT,SLB,"
        "COP,NOC,GD,MDT,ELV,CI,"
        # Semis and hardware (high beta, deep weeklies)
        "INTC,TSM,ASML,LRCX,MRVL,ON,SWKS,MCHP,NXPI,TER,SMCI,ARM,ANET,"
        "MPWR,GFS,WDC,STX,DELL,HPQ,HPE,"
        # High-growth software / internet
        "PLTR,SHOP,SNAP,ROKU,PINS,NET,DDOG,CRWD,ZS,OKTA,MDB,SNOW,RBLX,"
        "U,DOCU,ZM,TEAM,WDAY,HUBS,VEEV,TWLO,PATH,TTD,SPOT,RDDT,FTNT,"
        # Fintech / crypto-linked
        "COIN,HOOD,SOFI,AFRM,UPST,MARA,RIOT,MSTR,SQ,XYZ,"
        # EV and autos
        "RIVN,LCID,NIO,XPEV,LI,F,GM,"
        # Airlines, travel and leisure
        "UAL,DAL,AAL,LUV,ABNB,EXPE,MAR,HLT,CCL,RCL,NCLH,DKNG,PENN,"
        "CZR,MGM,WYNN,LVS,"
        # Consumer / retail momentum names
        "DASH,LYFT,CHWY,ETSY,W,RH,LULU,CMG,DPZ,YUM,MNST,CVNA,GME,AMC,"
        # Biotech and healthcare
        "MRNA,BNTX,ILMN,BIIB,ALNY,CVS,HIMS,DXCM,GEHC,"
        # China ADRs
        "BABA,JD,PDD,BIDU,NTES,"
        # Energy
        "OXY,DVN,FANG,HAL,MRO,APA,KMI,WMB,CVX,"
        # Media
        "WBD,PARA,LYV,"
        # Index and sector ETFs (cheap, deeply liquid chains)
        "SPY,QQQ,DIA,IWM,SOXL,TQQQ,SQQQ,ARKK,XLF,XLE,XLK,GLD,SLV,TLT"
    )
    popular_stock_max_spread_percent: Decimal = Field(
        default=Decimal("0.50"),
        ge=0,
        le=Decimal("10"),
    )
    top_gainers_limit: int = Field(default=200, ge=0, le=5000)
    # By request: "get the top gainers before the day starts and look
    # to invest in that for quick profit." Distinct from
    # top_gainers_limit above (Webull's default DAY_1/regular-session
    # ranking, anonymously folded into the whole universe) - Webull's
    # screener also supports rank_type="PRE_MARKET" directly (today's
    # biggest pre-market movers specifically), fetched once/day and
    # fed into seed_popular_symbols + force-scanned every cycle - see
    # AutoTrader.refresh_premarket_gainers. Smaller default than
    # top_gainers_limit - a curated priority list, not a universe-
    # filler.
    premarket_gainers_limit: int = Field(default=50, ge=0, le=1000)

    def stocks(self) -> list[str]:
        return [item.strip().upper() for item in self.stock_symbols.split(",") if item.strip()]

    def stock_universe_limit(self) -> int:
        return self.max_symbols or 500

    def stock_universe_pool(self) -> int:
        return self.stock_universe_limit() + self.stock_universe_reserve

    def exact_options(self) -> list[str]:
        return [item.strip().upper() for item in self.option_contracts.split(",") if item.strip()]

    def popular_stocks(self) -> list[str]:
        return [
            item.strip().upper()
            for item in self.popular_stock_symbols.split(",")
            if item.strip()
        ]

    def option_candidates(self) -> list[str]:
        return [
            item.strip().upper()
            for item in self.option_candidate_symbols.split(",")
            if item.strip()
        ]

    def default_watchlist(self) -> list[str]:
        return [
            item.strip().upper()
            for item in self.default_watchlist_symbols.split(",")
            if item.strip()
        ]

    def option_roots(self) -> list[str]:
        return [item.strip().upper() for item in self.option_underlyings.split(",") if item.strip()]
