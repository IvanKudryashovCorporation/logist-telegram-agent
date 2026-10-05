"""Кнопка «Обратная связь» вверху ленты."""

from app.config import settings


async def test_header_button_leads_to_the_owner_chat_on_every_page(client, make_order):
    order = await make_order()

    for path in ("/", f"/orders/{order.id}", "/my"):
        page = await client.get(path)
        assert "Обратная связь" in page.text, path
        assert f'href="https://t.me/{settings.owner_contact_username}"' in page.text, path


async def test_button_sits_in_the_header_and_not_in_a_footer(client, make_order):
    await make_order()

    page = (await client.get("/")).text

    assert "site-footer" not in page and 'class="feed-top"' not in page
    assert page.index("Обратная связь") < page.index("</header>")
    assert page.index("Обратная связь") < page.index('class="grid"')


async def test_button_is_hidden_when_no_contact_is_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "owner_contact_username", "")

    assert "Обратная связь" not in (await client.get("/")).text


async def test_at_sign_in_the_setting_is_tolerated(client, monkeypatch):
    monkeypatch.setattr(settings, "owner_contact_username", " @ivan_k ")

    assert 'href="https://t.me/ivan_k"' in (await client.get("/")).text
