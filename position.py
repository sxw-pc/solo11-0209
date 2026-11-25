import json
import struct
from itertools import combinations

from store import one, require

# 定长定位负载：有符号 int16 x、有符号 int16 y、无符号字节误差（1..8）。
PAYLOAD_LEN = 5
LEDGER_BATCH = '$ledger'
LEDGER_ORIG = 'fix-event'


def distance(x, y, a, b):
    return abs(x-a) + abs(y-b)


def envelope(db, buoy, at, since=0):
    fixes = {}
    for source, minute, x, y, error in db.execute('''SELECT source,minute,x,y,error FROM fixes
            WHERE buoy=? AND minute BETWEEN ? AND ? ORDER BY minute DESC,source''', (buoy, max(since, at-20), at)):
        fixes.setdefault(source, (minute, x, y, error))
    if len(fixes) < 2:
        return 'unconfirmed', None
    latest, x, y, _ = next(iter(fixes.values()))
    oldest = min(t for t, *_ in fixes.values())
    if latest-oldest > 10 or any(distance(a,b,c,d) > e+f
                                 for (_,a,b,e), (_,c,d,f) in combinations(fixes.values(), 2)):
        return 'conflicting', None
    radius = max(distance(x,y,a,b)+error for _,a,b,error in fixes.values()) + (at-oldest)//5
    return 'trusted', (x,y,radius)


# ---------------------------------------------------------------------------
# 定长负载编解码
# ---------------------------------------------------------------------------

def encode_payload(x, y, error):
    """测试与夹具使用的规范编码器：>hhB（有符号坐标 + 误差字节）。"""
    require(-32768 <= x <= 32767 and -32768 <= y <= 32767, 'coordinate outside int16')
    require(1 <= error <= 8, 'error outside field range')
    return struct.pack('>hhB', x, y, error)


def decode_payload(payload, length=PAYLOAD_LEN):
    if isinstance(payload, str):
        payload = bytes.fromhex(payload)
    require(isinstance(payload, (bytes, bytearray)), 'payload must be bytes')
    require(len(payload) == length, f'payload must be {length} bytes')
    x, y, error = struct.unpack('>hhB', bytes(payload))
    require(1 <= error <= 8, 'payload error outside 1..8')
    return x, y, error


def _as_bytes(payload, length):
    if isinstance(payload, str):
        try:
            payload = bytes.fromhex(payload)
        except ValueError as exc:
            raise ValueError('payload must be hex bytes') from exc
    require(isinstance(payload, (bytes, bytearray)), 'payload must be bytes or hex')
    require(len(payload) == length, f'payload must be {length} bytes')
    return bytes(payload)


# ---------------------------------------------------------------------------
# 批次目录、接收原件、拼校包入口
# ---------------------------------------------------------------------------

def open_batch(db, batch, length=PAYLOAD_LEN):
    require(isinstance(batch, str) and batch.strip(), 'batch id required')
    require(isinstance(length, int) and 1 <= length <= 64, 'bad payload length')
    row = one(db, 'SELECT length FROM packet_batches WHERE batch=?', batch)
    if row:
        require(row[0] == length, 'batch payload length mismatch')
        return
    db.execute('INSERT INTO packet_batches(batch,length,state) VALUES(?,?,?)', (batch, length, 'open'))


def add_catalog(db, batch, entries):
    """entries: [[orig, buoy, minute, source], ...]，固定报头。"""
    length = one(db, 'SELECT length FROM packet_batches WHERE batch=?', batch)
    require(length is not None, 'unknown packet batch')
    require(isinstance(entries, list) and entries, 'catalog entries required')
    for entry in entries:
        require(isinstance(entry, (list, tuple)) and len(entry) == 4, 'bad catalog header')
        orig, buoy, minute, source = entry
        require(isinstance(orig, str) and orig.strip(), 'orig id required')
        require(isinstance(source, str) and source.strip(), 'source required')
        require(isinstance(minute, int) and minute >= 0, 'minute must be non-negative int')
        require(one(db, 'SELECT 1 FROM buoys WHERE id=?', buoy) is not None, f'unknown buoy {buoy}')
        old = one(db, 'SELECT buoy,minute,source FROM packet_catalog WHERE batch=? AND orig=?', batch, orig)
        if old:
            require(old == (buoy, minute, source), f'header mismatch for {orig}')
            continue
        db.execute('INSERT INTO packet_catalog VALUES(?,?,?,?,?)', (batch, orig, buoy, minute, source))


def receive_original(db, batch, orig, payload, via='original'):
    """采录一个实际收到的原件（含重传）。同号同内容幂等，异内容留冲突。"""
    row = one(db, 'SELECT length FROM packet_batches WHERE batch=?', batch)
    require(row is not None, 'unknown packet batch')
    length = row[0]
    require(one(db, 'SELECT 1 FROM packet_catalog WHERE batch=? AND orig=?', batch, orig),
            f'orig {orig} not in batch catalog')
    blob = _as_bytes(payload, length)
    db.execute('''INSERT OR IGNORE INTO packet_originals(batch,orig,payload,payload_hex,via)
                  VALUES(?,?,?,?,?)''', (batch, orig, blob, blob.hex(), via))
    rebuild_batch(db, batch)
    return blob


def add_parity(db, evidence, batch, members, payload, via='parity'):
    """采录拼校包：evidence 为独立证据号，members 为一组同批原件号。"""
    require(isinstance(evidence, str) and evidence.strip(), 'parity evidence id required')
    brow = one(db, 'SELECT length FROM packet_batches WHERE batch=?', batch)
    require(brow is not None, 'unknown packet batch')
    length = brow[0]
    blob = _as_bytes(payload, length)
    require(isinstance(members, (list, tuple)) and members, 'parity members required')
    members = [str(m) for m in members]
    require(len(members) == len(set(members)), 'parity member repeated')
    for member in members:
        inside = one(db, 'SELECT 1 FROM packet_catalog WHERE batch=? AND orig=?', batch, member)
        require(inside is not None, f'parity {evidence} crosses batch or unknown orig {member}')
    canonical = json.dumps(sorted(members), ensure_ascii=False)
    db.execute('''INSERT OR IGNORE INTO packet_parities(evidence,batch,members,xor_hex,xor_bytes,via)
                  VALUES(?,?,?,?,?,?)''', (evidence, batch, canonical, blob.hex(), blob, via))
    rebuild_batch(db, batch)
    return blob


def request_retransmit(db, batch, orig, payload):
    """接收中心一次只补一个原件。"""
    return receive_original(db, batch, orig, payload, via='retransmit')


# ---------------------------------------------------------------------------
# 查询：原始负载 / 推导负载 / 冲突，分别可查、可回查
# ---------------------------------------------------------------------------

def received_payload(db, batch, orig):
    row = one(db, 'SELECT payload FROM packet_originals WHERE batch=? AND orig=? ORDER BY rowid LIMIT 1',
              batch, orig)
    return bytes(row[0]) if row else None


def received_versions(db, batch, orig):
    return [bytes(r[0]) for r in db.execute(
        'SELECT payload FROM packet_originals WHERE batch=? AND orig=? ORDER BY rowid', (batch, orig))]


def derived_payload(db, batch, orig):
    row = one(db, 'SELECT payload_hex FROM packet_derived WHERE batch=? AND orig=? AND state=\'derived\'',
              batch, orig)
    return bytes.fromhex(row[0]) if row else None


def conflict_rows(db, batch=None, buoy=None):
    sql = 'SELECT DISTINCT c.batch,c.code,c.refs,c.detail FROM packet_conflicts c'
    args = []
    if buoy is not None:
        sql += ' JOIN packet_catalog k ON k.batch=c.batch WHERE k.buoy=?'
        args.append(buoy)
        if batch is not None:
            sql += ' AND c.batch=?'
            args.append(batch)
    elif batch is not None:
        sql += ' WHERE c.batch=?'
        args.append(batch)
    sql += ' ORDER BY c.id'
    out = []
    for b, code, refs, detail in db.execute(sql, args):
        out.append({'batch': b, 'code': code, 'refs': json.loads(refs), 'detail': detail})
    return out


def conflict_note(db, buoy):
    rows = conflict_rows(db, buoy=buoy)
    if not rows:
        return ''
    refs = []
    for item in rows:
        for ref in item['refs']:
            if ref not in refs:
                refs.append(ref)
    return '; recovery evidence conflict (' + ','.join(refs) + ')'


def batch_status(db, batch):
    brow = one(db, 'SELECT length,state FROM packet_batches WHERE batch=?', batch)
    if brow is None:
        return None
    length, state = brow
    catalog = db.execute('SELECT orig FROM packet_catalog WHERE batch=? ORDER BY orig', (batch,)).fetchall()
    origs = [r[0] for r in catalog]
    received = {}
    divergent = []
    for orig in origs:
        versions = db.execute('SELECT DISTINCT payload_hex FROM packet_originals WHERE batch=? AND orig=?',
                              (batch, orig)).fetchall()
        if versions:
            received[orig] = versions[0][0]
            if len(versions) > 1:
                divergent.append(orig)
    derived = {}
    for orig, hexpayload, witnesses, dstate in db.execute(
            'SELECT orig,payload_hex,witnesses,state FROM packet_derived WHERE batch=? ORDER BY orig', (batch,)):
        derived[orig] = {'payload': hexpayload, 'witnesses': json.loads(witnesses), 'state': dstate}
    fixed = {o for o, d in derived.items() if d['state'] == 'derived'}
    pending = [o for o in origs if o not in received and o not in fixed]
    return {
        'length': length,
        'state': state,
        'catalog': origs,
        'received': received,
        'divergent': divergent,
        'derived': derived,
        'pending': pending,
        'conflicts': conflict_rows(db, batch=batch),
    }


# ---------------------------------------------------------------------------
# GF(2) 联立求解：把每个原件视为整串位向量，系数矩阵在每个位平面相同，
# 因此用 Python 大整数同时对所有字节做高斯消元，绝不枚举候选字节串。
# ---------------------------------------------------------------------------

def _solve_system(unknowns, equations, length):
    """equations: [(frozenset/member orig iterable, xor bytes, evidence id), ...]
    已知原件的负载先代入右端。返回 (fixed {orig: bytes}, conflicts [refs...])。
    每一行内部表示为 (coeff bitset, rhs big-int, witnesses set)。
    """
    index = {orig: i for i, orig in enumerate(unknowns)}
    rows = []
    for members, xor_blob, evidence, known_payloads in equations:
        coeff = 0
        rhs = int.from_bytes(xor_blob, 'big')
        witnesses = {evidence}
        for member in members:
            if member in index:
                coeff |= 1 << index[member]
            else:
                # 已收到的原件作为常数代入，并留下原件号回查。
                rhs ^= int.from_bytes(known_payloads[member], 'big')
                witnesses.add(member)
        rows.append([coeff, rhs, witnesses])

    m = len(unknowns)
    pivot_row = {}
    r = 0
    for col in range(m):
        pivot = next((i for i in range(r, len(rows)) if (rows[i][0] >> col) & 1), None)
        if pivot is None:
            continue
        rows[r], rows[pivot] = rows[pivot], rows[r]
        for i in range(len(rows)):
            if i != r and ((rows[i][0] >> col) & 1):
                rows[i][0] ^= rows[r][0]
                rows[i][1] ^= rows[r][1]
                rows[i][2] |= rows[r][2]
        pivot_row[col] = r
        r += 1

    conflicts = []
    for coeff, rhs, witnesses in rows:
        if coeff == 0 and rhs != 0:
            refs = sorted(witnesses)
            conflicts.append(refs)

    fixed = {}
    if not conflicts:
        width = length * 8
        mask = (1 << width) - 1
        for col, ri in pivot_row.items():
            coeff, rhs, witnesses = rows[ri]
            # 主元行除本变量外不含任何自由变量，该原件才在所有可行分配下唯一。
            if coeff == (1 << col):
                fixed[unknowns[col]] = ((rhs & mask).to_bytes(length, 'big'), sorted(witnesses))
    return fixed, conflicts


def _flag_conflict(db, batch, code, refs, detail):
    refs = sorted({str(r) for r in refs})
    signature = json.dumps([code, refs, detail], ensure_ascii=False, sort_keys=True)
    db.execute('''INSERT OR IGNORE INTO packet_conflicts(batch,code,refs,detail,signature)
                  VALUES(?,?,?,?,?)''', (batch, code, json.dumps(refs, ensure_ascii=False), detail, signature))
    db.execute("UPDATE packet_batches SET state='conflict' WHERE batch=?", (batch,))


def _sync_fix(db, buoy, source, minute):
    """让 fixes 表恰好等于当前仍 active 的支撑证据。

    已存在的 fix 若能被某条 active 支撑逐字段印证则保留（原始/既有结论优先）；
    否则它必是已撤回的推导行，删除后用最佳 active 支撑重建。
    """
    existing = one(db, 'SELECT x,y,error FROM fixes WHERE buoy=? AND source=? AND minute=?',
                   buoy, source, minute)
    supports = db.execute('''SELECT x,y,error,payload_hex FROM fix_support
                             WHERE buoy=? AND source=? AND minute=? AND state='active'
                             ORDER BY CASE kind WHEN 'raw' THEN 0 ELSE 1 END, batch, orig''',
                          (buoy, source, minute)).fetchall()
    if existing is not None:
        if any(s[0] is not None and (s[0], s[1], s[2]) == existing for s in supports):
            return
        db.execute('DELETE FROM fixes WHERE buoy=? AND source=? AND minute=?', (buoy, source, minute))
    valid = next((s for s in supports if s[0] is not None), None)
    if valid is not None and one(db, 'SELECT 1 FROM fixes WHERE buoy=? AND source=? AND minute=?',
                                 buoy, source, minute) is None:
        x, y, error, _ = valid
        db.execute('INSERT INTO fixes(buoy,source,minute,x,y,error) VALUES(?,?,?,?,?,?)',
                   (buoy, source, minute, x, y, error))


def rebuild_batch(db, batch):
    """对整批证据联立判断，并把唯一、合法的负载同步进现有 fixes 取证表。"""
    brow = one(db, 'SELECT length,state FROM packet_batches WHERE batch=?', batch)
    if brow is None:
        return None
    length, state = brow
    catalog = db.execute('''SELECT orig,buoy,minute,source FROM packet_catalog
                            WHERE batch=? ORDER BY orig''', (batch,)).fetchall()
    headers = {orig: (buoy, minute, source) for orig, buoy, minute, source in catalog}

    # 原件重传异内容 -> 冲突（同内容由幂等主键去重）。
    raw = {}
    for orig, *_ in catalog:
        versions = db.execute('SELECT payload FROM packet_originals WHERE batch=? AND orig=? ORDER BY rowid',
                              (batch, orig)).fetchall()
        distinct = []
        for (blob,) in versions:
            if not distinct or blob not in distinct:
                distinct.append(blob)
        if distinct:
            raw[orig] = distinct[0]
        if len(distinct) > 1:
            _flag_conflict(db, batch, 'original-reprint', [orig],
                           f'{orig} retransmitted with different payload')

    # 拼校包同证据号异内容 -> 冲突。
    parity_rows = db.execute('''SELECT evidence,members,xor_hex,xor_bytes FROM packet_parities
                                WHERE batch=? ORDER BY evidence,rowid''', (batch,)).fetchall()
    parities = {}
    for evidence, members_json, xor_hex, xor_bytes in parity_rows:
        item = (members_json, xor_hex)
        parities.setdefault(evidence, [])
        if item not in [(m, x) for m, x, _ in parities[evidence]]:
            parities[evidence].append((members_json, xor_hex, xor_bytes))
    equations = []
    for evidence, copies in parities.items():
        if len(copies) > 1:
            _flag_conflict(db, batch, 'parity-reprint', [evidence],
                           f'parity {evidence} retransmitted with different content')
        members_json, _xor_hex, xor_bytes = copies[0]
        equations.append((json.loads(members_json), bytes(xor_bytes), evidence))

    conflicted = one(db, "SELECT state FROM packet_batches WHERE batch=?", batch)[0] == 'conflict'

    fixed = {}
    if not conflicted:
        unknowns = [orig for orig in headers if orig not in raw]
        eq_with_known = [(members, xor_blob, evidence, raw) for members, xor_blob, evidence in equations]
        solved, bad_rows = _solve_system(unknowns, eq_with_known, length)
        if bad_rows:
            for refs in bad_rows:
                _flag_conflict(db, batch, 'infeasible-parity', refs,
                               'no feasible payload assignment satisfies the batch evidence')
            conflicted = True
        else:
            fixed = solved

    # 重建本批 fix_support（原始支撑始终保留；推导支撑仅在批次无冲突时 active）。
    db.execute('DELETE FROM fix_support WHERE batch=?', (batch,))
    for orig, (buoy, minute, source) in headers.items():
        if orig in raw:
            blob = raw[orig]
            try:
                x, y, error = decode_payload(blob, length)
                state_name, vals = 'active', (x, y, error)
            except ValueError:
                state_name, vals = 'rejected', (None, None, None)
            db.execute('''INSERT INTO fix_support(buoy,source,minute,kind,batch,orig,x,y,error,payload_hex,witnesses,state)
                          VALUES(?,?,?, 'raw', ?,?,?,?,?,?, ?,?)''',
                       (buoy, source, minute, batch, orig, *vals, blob.hex(),
                        json.dumps([orig]), state_name))

    if conflicted:
        # 整批推导立即失效：保留 packet_derived 历史行但标记 withdrawn。
        db.execute("UPDATE packet_derived SET state='withdrawn' WHERE batch=?", (batch,))
    else:
        db.execute('DELETE FROM packet_derived WHERE batch=?', (batch,))
        for orig in sorted(fixed):
            blob, witnesses = fixed[orig]
            db.execute('INSERT INTO packet_derived(batch,orig,payload_hex,witnesses,state) VALUES(?,?,?,?,?)',
                       (batch, orig, blob.hex(), json.dumps(witnesses), 'derived'))
            buoy, minute, source = headers[orig]
            try:
                x, y, error = decode_payload(blob, length)
                state_name, vals = 'active', (x, y, error)
            except ValueError:
                state_name, vals = 'rejected', (None, None, None)
            db.execute('''INSERT INTO fix_support(buoy,source,minute,kind,batch,orig,x,y,error,payload_hex,witnesses,state)
                          VALUES(?,?,?, 'derived', ?,?,?,?,?,?, ?,?)''',
                       (buoy, source, minute, batch, orig, *vals, blob.hex(),
                        json.dumps(witnesses), state_name))

    for orig, (buoy, minute, source) in headers.items():
        _sync_fix(db, buoy, source, minute)

    return batch_status(db, batch)


def apply(db, op, args):
    if op == 'buoy':
        db.execute('INSERT INTO buoys VALUES(?,?,?,?)', args)
    elif op == 'fix':
        buoy, source, minute, x, y, error = args
        # 先保持原有重复主键抛 sqlite3.IntegrityError 的行为。
        db.execute('INSERT INTO fixes VALUES(?,?,?,?,?,?)', args)
        db.execute('''INSERT INTO fix_support(buoy,source,minute,kind,batch,orig,x,y,error,payload_hex,witnesses,state)
                      VALUES(?,?,?, 'raw', ?,?,?,?,?,?, '[]', 'active')''',
                   (buoy, source, minute, LEDGER_BATCH, LEDGER_ORIG + ':' + source,
                    x, y, error, None))
    elif op == 'packet-batch':
        batch, *rest = args
        open_batch(db, batch, rest[0] if rest else PAYLOAD_LEN)
    elif op == 'packet-catalog':
        batch, entries = args
        open_batch(db, batch)
        add_catalog(db, batch, entries)
    elif op in ('packet-original', 'packet-retransmit'):
        batch, orig, payload = args
        receive_original(db, batch, orig, payload, via=op)
    elif op == 'packet-parity':
        evidence, batch, members, payload = args
        add_parity(db, evidence, batch, members, payload, via=op)
    else:  # pragma: no cover - 分发层在 buoy_recovery 已拦截未知操作
        raise ValueError(f'unknown position operation {op}')
