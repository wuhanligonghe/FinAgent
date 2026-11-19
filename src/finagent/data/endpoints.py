"""Fixed public source endpoints, migrated from Trading; never model-supplied URLs."""

TENCENT_QUOTE = "https://qt.gtimg.cn/"
SINA_QUOTE = "https://hq.sinajs.cn/"
SINA_DAILY = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
SINA_FINANCIALS = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
SINA_PROFILE = "https://vip.stock.finance.sina.com.cn/corp/go.php/"
SINA_NEWS = "https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php"
SINA_INDUSTRY = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
EASTMONEY_NEWS = "https://search-api-web.eastmoney.com/search/jsonp"
EASTMONEY_SEARCH = "https://searchapi.eastmoney.com/api/suggest/get"
ALLOWED_HOSTS = {"qt.gtimg.cn", "hq.sinajs.cn", "money.finance.sina.com.cn", "quotes.sina.cn",
                 "vip.stock.finance.sina.com.cn", "search-api-web.eastmoney.com", "searchapi.eastmoney.com"}
