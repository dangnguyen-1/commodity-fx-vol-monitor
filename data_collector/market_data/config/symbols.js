const futures = [
  'NYMEX:BZ1!',
  'NYMEX:CL1!',
  'NYMEX:NG1!',
  'NYMEX:RB1!',
  'ICEEUR:NCF1!',
  'COMEX:GC1!',
  'COMEX:SI1!',
  'SGX:FEF1!',
  'COMEX:HG1!',
  'COMEX:ALI1!',
  'RUS:ZC1!',
  'RUS:NC1!',
  'NYMEX:PL1!',
  'NYMEX:PA1!',
  'CBOT:ZS1!',
  'CBOT:ZW1!',
  'CBOT:ZC1!',
  'ICEUS:SB1!',
  'ICEUS:KC1!',
  'ICEUS:CT1!',
  'ICEUS:CC1!',
  'CME:LE1!',
  'CME:LBR1!',
  'NYMEX:HO1!',
  'ABAXX:GOM1!',
  'COMEX:LTH1!',
  'COMEX:UX1!',
  // ICE Rotterdam API2, the seaborne thermal coal benchmark. Newcastle
  // (NYMEX:QLA1!, NYMEX:MTF1!) is the Asian reference and would suit AUD
  // better, but neither resolves on this account; API2 tracks it closely
  // and carries full history back to 2010.
  'ICEEUR:ATW1!',
];

const proxies = [
  'AMEX:LIT',
];

const fx = [
  'FX:EURUSD',
  'FX:GBPUSD',
  'FX:NZDUSD',
  'FX:AUDUSD',
  'FX_IDC:BRLUSD',
  'FX:EURGBP',
  'FX:EURJPY',
  'FX:EURCHF',
  'FX:EURCAD',
  'FX:EURAUD',
  'FX:EURNZD',
  'FX:GBPJPY',
  'FX:GBPCHF',
  'FX:GBPCAD',
  'FX:GBPAUD',
  'FX:GBPNZD',
  'FX:AUDJPY',
  'FX:AUDNZD',
  'FX:AUDCAD',
  'FX:NZDJPY',
  'FX:NZDCAD',
  'FX:CADJPY',
  'FX:CHFJPY',
  'FX:USDJPY',
  'FX:USDCHF',
  'FX:USDCAD',
  // Commodity-currency dashboard coverage, all confirmed live on
  // TradingView's FX_IDC provider (see generate_fx_inverses.py for the
  // USDxxx -> xxxUSD inversion each of these needs).
  'FX_IDC:USDNOK',
  'FX_IDC:USDRUB',
  'FX_IDC:USDMXN',
  'FX_IDC:USDCOP',
  'FX_IDC:USDZAR',
  'FX_IDC:USDPEN',
  'FX_IDC:USDCLP',
  'FX_IDC:USDARS',
  'FX_IDC:USDUAH',
  'FX_IDC:USDKZT',
  'FX_IDC:USDCDF',
  'FX_IDC:USDZMW',
  'FX_IDC:USDGHS',
  'FX_IDC:USDPYG',
];

module.exports = {
  futures,
  fx,
  proxies,
};