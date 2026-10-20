"""Daily attribution placement. Missing/unverified channel info fails closed.

Provider T&C section 7.1 permits a visible About section. Removing the inline
credit is allowed only after a fresh getChat confirms the exact channel and
the required credit as a separate description line. No cached permission flag.
This module never writes channel info or publishes a message.
"""
import re

PROVIDER = 'football-data.org'
CREDIT = 'Football data provided by the Football-Data.org API'
TERMS = 'https://www.football-data.org/about'


def channel_credit_verified(chat, channel):
    if not isinstance(chat, dict) or chat.get('type') != 'channel':
        return False
    # Numeric chat IDs are deliberately not guessed from usernames.
    if not isinstance(channel, str) or not channel.startswith('@'):
        return False
    if str(chat.get('username', '')).casefold() != channel[1:].casefold():
        return False
    return CREDIT in str(chat.get('description', '')).splitlines()


def attribution_metadata(*, uses_provider, channel_verified=False):
    return dict(provider=PROVIDER, used=bool(uses_provider),
                credit=CREDIT, terms=TERMS, terms_section='7.1',
                placement=('CHANNEL_DESCRIPTION' if channel_verified else 'INLINE') if uses_provider else 'NOT_USED',
                channel_description_verified=bool(channel_verified))


def compose(blocks, *, uses_provider=False, channel_verified=False):
    """Format generated blocks, not arbitrary SOURCE prose or factual tokens."""
    parts = [block.strip() for block in blocks if block.strip()]
    # Only strip standalone technical footer blocks, never words inside facts.
    parts = [p for p in parts if p not in (CREDIT, CREDIT+' -', '@PremWire', '-', '—')]
    if uses_provider and not channel_verified:
        parts.append(CREDIT)
    parts.append('@PremWire')
    return '\n\n'.join(re.sub(r'[ \t]{2,}', ' ', p) for p in parts)
