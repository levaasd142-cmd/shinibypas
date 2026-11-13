"""USDT mainnet identities and read-only verification for TON, TRON and Aptos.

No signing or custody. Each `NetworkVerifier.verify_<network>` docstring lists its steps.
Exceptions follow bep20.py: PaymentMismatch = definite mismatch (uses an attempt),
PendingPayment = temporary (no attempt used). Any unexpected error inside verify() is
converted to PendingPayment.
"""
import base64
import binascii
import hashlib
import re
import time
from decimal import Decimal

import aiohttp

from bep20 import (WALLET, TOKEN, TRANSFER, PendingPayment, PaymentMismatch, VerifiedTransfer,
                   MSG_NODE_UNAVAILABLE, MSG_NOT_FOUND, MSG_NO_TRANSFER, fetch_json, guarded,
                   throttle, address as bsc_address)

__all__ = ['NETWORKS', 'NetworkVerifier', 'PendingPayment', 'PaymentMismatch', 'VerifiedTransfer',
           'normalize_address', 'normalize_hash', 'amount', 'qr', 'tron_hex', 'tron_friendly',
           'ton_raw', 'aptos_address', 'ton_jetton_wallet']

NETWORKS = {
    'bsc': dict(label='BEP20', decimals=18, token=TOKEN.lower(), wallet=WALLET),
    'ton': dict(label='TON', decimals=6, token='EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs', wallet=''),
    'tron': dict(label='TRC20', decimals=6, token='TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t', wallet=''),
    'aptos': dict(label='Aptos', decimals=6,
                  token='0x357b0b74bc833e95a115ad22604854d6b0fca151cecd94111770e5d6ffc9dc2b', wallet=''),
}
BASE58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'

TONCENTER = 'https://toncenter.com/api'
TRONGRID = 'https://api.trongrid.io'
APTOS = 'https://fullnode.mainnet.aptoslabs.com/v1'
TON_INTERNAL_TRANSFER = 0x178d4519
TON_INDEX_LAG = 600          # seconds a completed trace may wait for /jetton/transfers indexing

MSG_TX_FAILED = 'Транзакция неуспешна или не соответствует TXID'
MSG_WAIT_FINALITY = 'Ожидаем подтверждений сети. Проверка повторится автоматически.'
MSG_WAIT_TON_INDEX = 'Ожидаем индексации перевода TON.'
MSG_TON_NOT_RECEIVED = 'Получение USDT TON не подтверждено. Обратитесь в поддержку.'
MSG_WRONG_APTOS_CHAIN = 'Требуется Aptos mainnet'
MSG_BAD_TXID = 'TXID должен содержать 64 шестнадцатеричных символа'
MSG_BAD_TXID_TON = 'Неверный TXID TON'


# ---------------------------------------------------------------- addresses and hashes

def tron_hex(value):
    if re.fullmatch(r'41[0-9a-fA-F]{40}', value):
        return value.lower()
    if not re.fullmatch(r'T[1-9A-HJ-NP-Za-km-z]{33}', value):
        raise ValueError('Неверный адрес TRON')
    number = 0
    for char in value:
        number = number * 58 + BASE58.index(char)
    raw = number.to_bytes(25, 'big')
    checksum = hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4]
    if raw[0] != 0x41 or checksum != raw[-4:]:
        raise ValueError('Неверная контрольная сумма TRON')
    return raw[:21].hex()


def tron_friendly(value):
    raw = bytes.fromhex(tron_hex(value))
    raw += hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    number = int.from_bytes(raw, 'big')
    result = ''
    while number:
        number, remainder = divmod(number, 58)
        result = BASE58[remainder] + result
    return result


def ton_raw(value):
    """Any TON address form -> canonical raw 'wc:hex' (lowercase)."""
    if re.fullmatch(r'(0|-1):[0-9a-fA-F]{64}', value):
        return value.lower()
    try:
        raw = base64.urlsafe_b64decode(value + '=' * ((-len(value)) % 4))
        if (len(raw) != 36 or raw[0] not in (0x11, 0x51) or raw[1] != 0
                or binascii.crc_hqx(raw[:34], 0).to_bytes(2, 'big') != raw[34:]):
            raise ValueError
        return '0:' + raw[2:34].hex()
    except (ValueError, binascii.Error):
        raise ValueError('Неверный адрес TON mainnet') from None


def aptos_address(value):
    if not re.fullmatch(r'0x[0-9a-fA-F]{1,64}', value):
        raise ValueError('Неверный адрес Aptos')
    return '0x' + value[2:].lower().zfill(64)


def aptos_primary_store(owner, metadata):
    """Primary fungible store = object::create_user_derived_object_address(owner, metadata)."""
    seed = bytes.fromhex(aptos_address(owner)[2:]) + bytes.fromhex(aptos_address(metadata)[2:])
    return '0x' + hashlib.sha3_256(seed + b'\xfc').hexdigest()


def normalize_address(network, value):
    normalizers = {'bsc': bsc_address, 'ton': ton_raw, 'tron': tron_hex, 'aptos': aptos_address}
    return normalizers[network](value.strip())


def normalize_hash(network, value):
    """User TXID -> canonical form. Invalid input raises PaymentMismatch (a ValueError)."""
    value = value.strip()
    if network == 'ton' and not re.fullmatch(r'(0x)?[0-9a-fA-F]{64}', value):
        try:
            raw = base64.urlsafe_b64decode(value + '=' * ((-len(value)) % 4))
            if len(raw) != 32:
                raise ValueError
            return raw.hex()
        except (ValueError, binascii.Error):
            raise PaymentMismatch(MSG_BAD_TXID_TON) from None
    stripped = value.removeprefix('0x')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', stripped):
        raise PaymentMismatch(MSG_BAD_TXID)
    return ('0x' if network in ('bsc', 'aptos') else '') + stripped.lower()


def _ton_api_hash(value):
    """Hash taken from an API response: malformed data must stay a temporary error."""
    try:
        return normalize_hash('ton', value)
    except (PaymentMismatch, AttributeError, TypeError):
        raise ValueError('malformed hash in API response') from None


def amount(raw, network):
    decimals = NETWORKS[network]['decimals']
    # Preserve legacy BEP20 invoices with a six-decimal suffix.
    places = 6 if network == 'bsc' and int(raw) % 10**14 else 4
    return format(Decimal(raw) / Decimal(10**decimals), '.' + str(places) + 'f')


def qr(wallet):
    import io
    import qrcode
    output = io.BytesIO()
    qrcode.make(wallet).save(output, format='PNG')
    return output.getvalue()


# ---------------------------------------------------------------- minimal TON cells / BOC

def _crc32c(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def ton_address_slice_boc(address):
    """BOC (base64, with crc32c) of a single cell holding MsgAddressInt addr_std."""
    workchain, account = ton_raw(address).split(':')
    bits = '100' + format(int(workchain) & 0xFF, '08b') + format(int(account, 16), '0256b')  # 267 bits
    padded = bits + '1' + '0' * ((-len(bits) - 1) % 8)
    data = int(padded, 2).to_bytes(len(padded) // 8, 'big')
    descriptor = bytes([0, len(bits) // 8 + (len(bits) + 7) // 8])
    cell = descriptor + data
    # magic, flags(has_crc32c, size=1), off_bytes=1, cells=1, roots=1, absent=0, total size, root 0
    boc = bytes.fromhex('b5ee9c72') + bytes([0x41, 1, 1, 1, 0, len(cell), 0]) + cell
    return base64.b64encode(boc + _crc32c(boc).to_bytes(4, 'little')).decode()


def boc_root_bits(value):
    """Return the root cell's data of a base64 BOC as a '0'/'1' string."""
    data = base64.b64decode(value)
    if data[:4] != bytes.fromhex('b5ee9c72'):
        raise ValueError('bad boc magic')
    flags = data[4]
    has_index, size = flags & 0x80, flags & 0x07
    off_bytes = data[5]
    pos = 6

    def take(count):
        nonlocal pos
        chunk = data[pos:pos + count]
        if len(chunk) != count:
            raise ValueError('truncated boc')
        pos += count
        return int.from_bytes(chunk, 'big')

    cells, roots, _absent = take(size), take(size), take(size)
    take(off_bytes)                                   # total cells size
    root = take(size)
    take(size * (roots - 1))
    if has_index:
        take(off_bytes * cells)
    if root >= cells:
        raise ValueError('bad root')
    for index in range(cells):
        d1, d2 = take(1), take(1)
        if d1 & 16:                                   # stored hashes/depths
            take((bin(d1 >> 5).count('1') + 1) * 34)
        length = (d2 + 1) // 2
        cell = data[pos:pos + length]
        take(length)
        take(size * (d1 & 7))
        if index == root:
            if d1 & 8:
                raise ValueError('exotic root cell')
            bits = ''.join(format(byte, '08b') for byte in cell)
            if d2 % 2:                                # strip completion tag
                bits = bits.rstrip('0')[:-1]
            return bits
    raise ValueError('root not found')


def bits_to_ton_address(bits):
    if len(bits) < 267 or bits[:3] != '100':
        raise ValueError('not addr_std')
    workchain = int(bits[3:11], 2)
    workchain -= 256 if workchain > 127 else 0
    return f'{workchain}:{int(bits[11:267], 2):064x}'


def ton_internal_transfer_amount(body_boc):
    """Amount from an internal_transfer body (op, query_id, VarUInteger 16), else None."""
    try:
        bits = boc_root_bits(body_boc)
        if int(bits[:32], 2) != TON_INTERNAL_TRANSFER:
            return None
        length = int(bits[96:100], 2)
        field = bits[100:100 + length * 8]
        if len(field) != length * 8:
            return None
        return int(field, 2) if field else 0
    except Exception:
        return None


_ton_wallet_cache = {}       # (master_raw, owner_raw) -> jetton wallet raw address


def _stack_cell_value(entry):
    """Extract a BOC from a toncenter v3 {'type','value'} or v2 ['cell', {'bytes'}] entry."""
    if isinstance(entry, dict):
        if entry.get('type') not in ('cell', 'slice'):
            raise ValueError('unexpected stack entry')
        return entry['value']
    kind, value = entry
    if kind not in ('cell', 'slice', 'tvm.Cell', 'tvm.Slice'):
        raise ValueError('unexpected stack entry')
    return value['bytes'] if isinstance(value, dict) else value


async def ton_jetton_wallet(request, master, owner):
    """Derive owner's jetton wallet on-chain via the master's get_wallet_address(owner).

    `request(url, payload=None, params=None)` performs one throttled JSON call. Tries the
    toncenter v3 runGetMethod, then v2. Errors propagate as PendingPayment.
    """
    key = (ton_raw(master), ton_raw(owner))
    if key in _ton_wallet_cache:
        return _ton_wallet_cache[key]
    argument = ton_address_slice_boc(owner)
    calls = [
        (TONCENTER + '/v3/runGetMethod',
         {'address': master, 'method': 'get_wallet_address', 'stack': [{'type': 'slice', 'value': argument}]}),
        (TONCENTER + '/v2/runGetMethod',
         {'address': master, 'method': 'get_wallet_address', 'stack': [['tvm.Slice', argument]]}),
    ]
    for url, payload in calls:
        try:
            data = await request(url, payload=payload)
            result = data.get('result', data) if data.get('ok') is True else data
            if int(result['exit_code']) != 0 or len(result['stack']) != 1:
                raise ValueError('get method failed')
            wallet = bits_to_ton_address(boc_root_bits(_stack_cell_value(result['stack'][0])))
        except Exception:
            continue
        _ton_wallet_cache[key] = wallet
        return wallet
    raise PendingPayment(MSG_NODE_UNAVAILABLE)


# ---------------------------------------------------------------- verifier

class NetworkVerifier:
    """Read-only verifier for one of 'ton', 'tron', 'aptos' (BSC lives in bep20.py)."""

    # Minimum spacing between requests per network, shared by all verifier instances.
    INTERVALS = {('ton', False): 1.1, ('ton', True): 0.12, ('tron', False): 0.3}

    def __init__(self, network, api_key=''):
        self.network = network
        self.api_key = api_key
        self.session = None

    async def request(self, url, payload=None, params=None, not_found=MSG_NOT_FOUND):
        await throttle(self.network, self.INTERVALS.get((self.network, bool(self.api_key)), 0))
        headers = {}
        if self.api_key:
            headers['TRON-PRO-API-KEY' if self.network == 'tron' else 'X-API-Key'] = self.api_key
        data = await fetch_json(self.session, url, payload=payload, params=params, headers=headers,
                                not_found=not_found)
        if isinstance(data, dict) and (data.get('Error') or data.get('error') or data.get('ok') is False):
            raise PendingPayment(MSG_NODE_UNAVAILABLE)
        return data

    async def verify(self, txid, wallet, token, raw):
        txid = normalize_hash(self.network, txid)
        handler = getattr(self, 'verify_' + self.network)

        async def run():
            async with aiohttp.ClientSession() as session:
                self.session = session
                try:
                    return await handler(txid, wallet, token, str(int(raw)))
                finally:
                    self.session = None
        return await guarded(run(), 90)

    async def verify_tron(self, txid, wallet, token, raw):
        """TRON (TRC20):
        1. walletsolidity/gettransactioninfobyid - only solidified (final) txs are returned;
           empty answer = not final yet (pending).
        2. receipt.result must be SUCCESS.
        3. A Transfer log from the USDT contract to the shop wallet for exactly `raw`.
           transfer_id = "<txid>:<position of the log>".
        """
        data = await self.request(TRONGRID + '/walletsolidity/gettransactioninfobyid', {'value': txid})
        if not data:
            raise PendingPayment(MSG_WAIT_FINALITY)
        if data['id'] != txid:
            raise PendingPayment(MSG_NODE_UNAVAILABLE)
        if data.get('receipt', {}).get('result') != 'SUCCESS':
            raise PaymentMismatch(MSG_TX_FAILED)
        contract = tron_hex(token)[2:]
        recipient = '0' * 24 + tron_hex(wallet)[2:]
        matches = []
        for index, log in enumerate(data.get('log') or []):
            topics = [str(topic).lower() for topic in log.get('topics') or []]
            if (str(log.get('address', '')).lower() != contract or len(topics) != 3
                    or topics[0] != TRANSFER[2:] or topics[2] != recipient):
                continue
            if int(log['data'], 16) == int(raw):
                matches.append(index)
        if not matches:
            raise PaymentMismatch(MSG_NO_TRANSFER)
        return VerifiedTransfer(txid, wallet, token, raw, int(data['blockTimeStamp']) // 1000,
                                int(data['blockNumber']), txid, 'tron', f'{txid}:{matches[0]}')

    async def verify_aptos(self, txid, wallet, token, raw):
        """Aptos (fungible asset USDt):
        1. Ledger chain_id must be 1 (mainnet).
        2. transactions/by_hash: pending -> wait; must be a successful user_transaction.
        3. A 0x1::fungible_asset::Deposit event into the shop's primary store for the
           official USDt metadata (deterministic address binds recipient AND asset) for
           exactly `raw`. transfer_id = "<txid>:<event index>".
        """
        ledger = await self.request(APTOS)
        if int(ledger['chain_id']) != 1:
            raise PaymentMismatch(MSG_WRONG_APTOS_CHAIN)
        data = await self.request(APTOS + '/transactions/by_hash/' + txid)
        if data.get('type') == 'pending_transaction':
            raise PendingPayment(MSG_WAIT_FINALITY)
        if data['hash'] != txid:
            raise PendingPayment(MSG_NODE_UNAVAILABLE)
        if data.get('success') is not True or data.get('type') != 'user_transaction':
            raise PaymentMismatch(MSG_TX_FAILED)
        store = aptos_primary_store(wallet, token)
        matches = []
        for index, event in enumerate(data.get('events') or []):
            if event.get('type') != '0x1::fungible_asset::Deposit':
                continue
            if aptos_address(event['data']['store']) == store and int(event['data']['amount']) == int(raw):
                matches.append(index)
        if not matches:
            raise PaymentMismatch(MSG_NO_TRANSFER)
        return VerifiedTransfer(txid, wallet, token, raw, int(data['timestamp']) // 1000000,
                                int(data['version']), txid, 'aptos', f'{txid}:{matches[0]}')

    async def verify_ton(self, txid, wallet, token, raw):
        """TON (USDT jetton):
        1. /v3/traces?tx_hash: exactly one completed, finalized trace containing the TXID.
        2. Derive the shop's real USDT jetton wallet on-chain (master.get_wallet_address);
           never trust the /jetton/wallets index (anyone can deploy a look-alike contract).
        3. Candidate = trace tx on that jetton wallet processing internal_transfer
           (op 0x178d4519) that succeeded. Its amount comes from the message body; when
           the body is not provided it is matched against the /jetton/transfers index by
           source jetton wallet and amount.
        4. No match while the trace is younger than TON_INDEX_LAG -> pending (indexing).
        txid = trace id (canonical for every tx hash of the trace);
        transfer_id = hash of the receiving jetton-wallet transaction.
        """
        base = TONCENTER + '/v3'
        data = await self.request(base + '/traces', params={'tx_hash': txid, 'limit': 2})
        traces = data.get('traces') or []
        if len(traces) != 1:
            raise PendingPayment(MSG_WAIT_TON_INDEX)
        trace = traces[0]
        if trace.get('is_incomplete') is not False or int(trace.get('mc_seqno_end') or 0) < 1:
            raise PendingPayment(MSG_WAIT_FINALITY)
        transactions = list((trace.get('transactions') or {}).values())
        if not any(_ton_api_hash(tx['hash']) == txid for tx in transactions):
            raise PendingPayment(MSG_NODE_UNAVAILABLE)
        trace_id = _ton_api_hash(trace['trace_id'])

        shop_jetton_wallet = await ton_jetton_wallet(self.request, token, wallet)
        received = [tx for tx in transactions if self._ton_received(tx, shop_jetton_wallet)]
        received.sort(key=lambda tx: int(tx.get('lt') or 0))

        chosen = None
        unknown = []
        for tx in received:
            body = ((tx['in_msg'].get('message_content') or {}).get('body'))
            value = ton_internal_transfer_amount(body) if body else None
            if value == int(raw):
                chosen = tx
                break
            if value is None:
                unknown.append(tx)
        if chosen is None and unknown:
            chosen = await self._ton_match_index(base, trace, trace_id, unknown, wallet, token, raw)

        if chosen is None:
            if time.time() - int(trace['end_utime']) < TON_INDEX_LAG:
                raise PendingPayment(MSG_WAIT_TON_INDEX)
            raise PaymentMismatch(MSG_TON_NOT_RECEIVED if not received else MSG_NO_TRANSFER)
        return VerifiedTransfer(trace_id, wallet, token, raw, int(chosen['now']),
                                int(trace['mc_seqno_end']), trace_id, 'ton',
                                _ton_api_hash(chosen['hash']))

    @staticmethod
    def _ton_received(tx, jetton_wallet):
        """Successful (non-emulated) internal_transfer executed by the shop's jetton wallet.

        A bounced transfer_notification at the owner's address does not undo the credit,
        so only the jetton wallet's own transaction matters.
        """
        description = tx.get('description') or {}
        in_msg = tx.get('in_msg') or {}
        opcode = in_msg.get('opcode')
        try:
            opcode = int(str(opcode), 16) if isinstance(opcode, str) else int(opcode)
        except (TypeError, ValueError):
            return False
        return (ton_raw(tx['account']) == jetton_wallet and tx.get('emulated') is not True
                and description.get('aborted') is False
                and (description.get('compute_ph') or {}).get('success') is True
                and opcode == TON_INTERNAL_TRANSFER)

    async def _ton_match_index(self, base, trace, trace_id, candidates, wallet, token, raw):
        """Fallback: pair candidates with indexed jetton transfers (same trace, exact amount)."""
        rows = []
        for offset in range(0, 10000, 1000):
            data = await self.request(base + '/jetton/transfers', params={
                'owner_address': wallet, 'jetton_master': token, 'direction': 'in',
                'start_utime': max(0, int(trace['start_utime']) - 60),
                'end_utime': int(trace['end_utime']) + 60, 'limit': 1000, 'offset': offset})
            page = data.get('jetton_transfers') or []
            rows.extend(page)
            if len(page) < 1000:
                break
        else:
            raise PendingPayment(MSG_WAIT_TON_INDEX)
        sources = {ton_raw(row['source_wallet']) for row in rows
                   if _ton_api_hash(row['trace_id']) == trace_id
                   and row.get('transaction_aborted') is False
                   and ton_raw(row['destination']) == ton_raw(wallet)
                   and ton_raw(row['jetton_master']) == ton_raw(token)
                   and int(row['amount']) == int(raw)}
        paired = [tx for tx in candidates if ton_raw(tx['in_msg']['source']) in sources]
        by_source = {}
        for tx in candidates:
            by_source.setdefault(ton_raw(tx['in_msg']['source']), []).append(tx)
        for tx in paired:
            # Ambiguous when the same source sent several bodiless transfers here.
            if len(by_source[ton_raw(tx['in_msg']['source'])]) == 1:
                return tx
        if paired:
            raise PaymentMismatch(MSG_TON_NOT_RECEIVED)
        return None
