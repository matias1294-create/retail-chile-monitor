from monitor import Product, alert_reason, parse_candidate

def test_parse_discount():
    p=parse_candidate('Tienda','https://ejemplo.cl/product/123','Notebook Prueba','Notebook Prueba $ 199.990 -75% $ 799.990')
    assert p.current_price==199990 and p.reference_price==799990 and p.published_discount==75.0

def test_historical_drop():
    p=Product('Tienda','Notebook Prueba','https://ejemplo.cl/product/123',199990,None,None,'')
    should,meta=alert_reason(p,{'price':799990,'last_alert_price':None})
    assert should and meta['historical_drop']>74

def test_no_repeat():
    p=Product('Tienda','Polera Prueba','https://ejemplo.cl/product/123',9990,39990,75.0,'')
    should,_=alert_reason(p,{'price':9990,'last_alert_price':9990})
    assert not should
