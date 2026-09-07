"""静态面板路由：/ 返回 index.html，/static 提供 JS/CSS 资源。"""


async def test_index_serves_panel_html(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert 'x-data="tikuApp()"' in resp.text


async def test_static_app_js_is_served(client):
    resp = await client.get("/static/app.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


async def test_static_style_css_is_served(client):
    resp = await client.get("/static/style.css")
    assert resp.status_code == 200
    assert "css" in resp.headers["content-type"]


async def test_alpine_served_locally(client):
    """面板不依赖外部 CDN：Alpine 由本地 /static 提供（离线可用）。"""
    resp = await client.get("/static/alpine.min.js")
    assert resp.status_code == 200
    assert "Alpine" in resp.text or "alpine" in resp.text


async def test_root_supports_head(client):
    """OCS 题库连通性探测发 HEAD /（带 ?t= 缓存破坏参数），须回 200 而非 405。"""
    resp = await client.head("/", params={"t": "1788800025486"})
    assert resp.status_code == 200
