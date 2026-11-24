"""Pinned ISO 4217 currency minor units for deterministic monetary results.

Source: SIX, ISO 4217 Maintenance Agency, List One published 2026-09-17:
https://www.six-group.com/dam/download/financial-information/data-center/iso-currrency/lists/list-one.xml
Codes whose minor unit is N.A. are deliberately unsupported for cost totals.
Updates are reviewed data changes; runtime calculations never fetch a rate or
silently interpret an unknown currency as US cents.
"""


_MINOR_UNITS = {
    **dict.fromkeys("AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CNY COP COU CRC CUP CVE CZK DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GTQ GYD HKD HNL HTG HUF IDR ILS INR IRR JMD KES KGS KHR KPW KYD KZT LAK LBP LKR LRD LSL MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD PAB PEN PGK PHP PKR PLN QAR RON RSD RUB SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TOP TRY TTD TWD TZS UAH USD USN UYU UZS VED VES WST XAD XCD XCG YER ZAR ZMW ZWG".split(), 2),
    **dict.fromkeys("BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI VND VUV XAF XOF XPF".split(), 0),
    **dict.fromkeys("BHD IQD JOD KWD LYD OMR TND".split(), 3),
    **dict.fromkeys("CLF UYW".split(), 4),
}


def minor_units(currency: str) -> int:
    try:
        return _MINOR_UNITS[currency]
    except (KeyError, TypeError):
        raise ValueError("Currency has no supported ISO 4217 minor unit") from None
