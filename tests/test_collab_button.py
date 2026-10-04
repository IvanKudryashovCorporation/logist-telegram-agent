"""Кнопка «Для сотрудничества и предложений» внизу сайта."""

from app.config import settings


async def test_footer_button_leads_to_the_owner_chat(client, make_order):
    order = await make_order()

    for path in ("/", f"/orders/{order.id}"):
        page = await client.get(path)
        assert "Для сотрудничества и предложений" in page.text, path
        assert f'href="https://t.me/{settings.owner_contact_username}"' in page.text, path


async def test_button_is_hidden_when_no_contact_is_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "owner_contact_username", "")

    assert "Для сотрудничества и предложений" not in (await client.get("/")).text


async def test_at_sign_in_the_setting_is_tolerated(client, monkeypatch):
    monkeypatch.setattr(settings, "owner_contact_username", " @ivan_k ")

    assert 'href="https://t.me/ivan_k"' in (await client.get("/")).text
