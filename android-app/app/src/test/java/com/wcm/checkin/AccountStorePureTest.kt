package com.wcm.checkin

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** AccountStore 纯函数单元测试（不触 Android）：序列化往返 / 容错降级 / 合并式写回语义。 */
class AccountStorePureTest {

    private fun acc(
        uid: String,
        name: String = "账号$uid",
        cookie: String = "SUB=$uid",
        lastResult: String = "",
    ) = AccountStore.Account(uid, name, cookie, lastResult)

    // ---------- 序列化往返 ----------

    @Test
    fun serialize_deserialize_roundtrip() {
        val list = listOf(
            acc("1001", name = "小号", cookie = "SUB=a; SUBP=b", lastResult = "签到 3/3 · 12:00:00"),
            acc("1002", name = "", cookie = "SUB=c"),
        )
        val parsed = AccountStore.deserializeAccounts(AccountStore.serializeAccounts(list))
        assertEquals(2, parsed.size)
        assertEquals("1001", parsed[0].uid)
        assertEquals("小号", parsed[0].name)
        assertEquals("SUB=a; SUBP=b", parsed[0].cookie)
        assertEquals("签到 3/3 · 12:00:00", parsed[0].lastResult)
        assertEquals("1002", parsed[1].uid)
        assertEquals("SUB=c", parsed[1].cookie)
    }

    @Test
    fun deserialize_emptyAndMissingFields() {
        assertEquals(emptyList<AccountStore.Account>(), AccountStore.deserializeAccounts("[]"))

        val parsed = AccountStore.deserializeAccounts("""[{"uid":"1001","cookie":"SUB=a"}]""")
        assertEquals(1, parsed.size)
        // 缺 name 落默认值（与旧行为一致），缺 lastResult 落空串
        assertEquals("未命名账号", parsed[0].name)
        assertEquals("", parsed[0].lastResult)
    }

    @Test
    fun deserialize_malformed_fallsBackToEmpty() {
        // 存储损坏时静默降级为空列表，宁可丢账号也不能让 App 起不来
        assertEquals(emptyList<AccountStore.Account>(), AccountStore.deserializeAccounts("not-json"))
        assertEquals(emptyList<AccountStore.Account>(), AccountStore.deserializeAccounts("{}"))
    }

    // ---------- 合并式写回语义（Worker 与前台共用的防丢/防复活逻辑） ----------

    @Test
    fun mergeAccounts_overlaysCookieAndResultForExistingUid() {
        val current = listOf(
            acc("1001", cookie = "SUB=old", lastResult = "旧状态"),
            acc("1002", cookie = "SUB=b", lastResult = "未动"),
        )
        val snapshot = listOf(acc("1001", cookie = "SUB=new", lastResult = "签到 5/5"))

        val merged = AccountStore.mergeAccounts(current, snapshot)

        assertEquals(2, merged.size)
        assertEquals("SUB=new", merged[0].cookie)
        assertEquals("签到 5/5", merged[0].lastResult)
        assertEquals("SUB=b", merged[1].cookie)      // 快照里没有的账号原样保留
        assertEquals("未动", merged[1].lastResult)
    }

    @Test
    fun mergeAccounts_snapshotMembershipIgnored() {
        val current = listOf(acc("1001"))
        // 快照里多出的账号（运行期间用户刚加的，或已删账号的旧快照）不进入结果
        val snapshot = listOf(
            acc("1001", cookie = "SUB=new"),
            acc("9999", name = "刚添加"),
        )

        val merged = AccountStore.mergeAccounts(current, snapshot)

        assertEquals(listOf("1001"), merged.map { it.uid })
    }

    @Test
    fun mergeAccounts_nameAndUidNotOverwritten() {
        val current = listOf(acc("1001", name = "面板自定义名"))
        val snapshot = listOf(acc("1001", name = "另一个名字", cookie = "SUB=new"))

        val merged = AccountStore.mergeAccounts(current, snapshot)

        assertTrue(merged[0].name == "面板自定义名")
        assertEquals("1001", merged[0].uid)
    }

    @Test
    fun mergeAccounts_blankSnapshotUidNeverMatches() {
        val current = listOf(acc("1001", cookie = "SUB=keep"))
        val snapshot = listOf(acc("", cookie = "SUB=blank"))

        val merged = AccountStore.mergeAccounts(current, snapshot)

        assertEquals("SUB=keep", merged[0].cookie)
    }
}
