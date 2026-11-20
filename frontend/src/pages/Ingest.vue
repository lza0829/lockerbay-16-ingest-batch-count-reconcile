<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
const events = ref([])
const report = ref(null)
const batches = ref([])
const verifyCache = ref({})
const busy = ref(false)

async function load() {
  events.value = await api.events()
  batches.value = await api.ingestBatches()
}
async function run() {
  busy.value = true
  try {
    report.value = await api.ingest()
  } finally {
    busy.value = false
  }
  await load()
}
async function verify(id) {
  verifyCache.value[id] = await api.verifyIngestBatch(id)
}
function statusClass(s) {
  return s === 'committed' ? 'ok' : 'bad'
}
onMounted(load)
</script>
<template>
  <div>
    <h1>装载</h1>
    <p class="sub">投柜 / 取件事件进入主通道；每批先写业务变更，再按库内实际变化对账，账不平整批回滚。</p>
    <p><button :disabled="busy" @click="run">{{ busy ? '对账中…' : '跑一批夹具' }}</button></p>

    <div v-if="report" class="panel" style="margin-top:1rem">
      <h3>
        本批回执
        <span class="tag" :class="statusClass(report.status)">{{ report.status }}</span>
        <code>{{ report.batch_id }}</code>
      </h3>
      <table>
        <thead>
          <tr><th>events_inserted</th><th>occupancies_opened</th><th>occupancies_closed</th><th>held_count</th></tr>
        </thead>
        <tbody>
          <tr>
            <td>{{ report.report.events_inserted }}</td>
            <td>{{ report.report.occupancies_opened }}</td>
            <td>{{ report.report.occupancies_closed }}</td>
            <td>{{ report.report.held_count }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="report.status === 'reconcile_failed'" class="bad">
        对账失败已回滚，差异：<code>{{ report.report.mismatches }}</code>
      </p>
    </div>

    <div class="panel" style="margin-top:1rem; overflow:auto">
      <h3>批次对账快照（不可变）</h3>
      <table>
        <thead>
          <tr><th>批次</th><th>时刻</th><th>状态</th><th>事件</th><th>新开</th><th>关闭</th><th>搁置</th><th>核证</th></tr>
        </thead>
        <tbody>
          <tr v-for="b in batches" :key="b.batch_id">
            <td><code>{{ b.batch_id }}</code></td>
            <td>{{ b.created_at }}</td>
            <td><span class="tag" :class="statusClass(b.status)">{{ b.status }}</span></td>
            <td>{{ b.events_inserted }}</td>
            <td>{{ b.occupancies_opened }}</td>
            <td>{{ b.occupancies_closed }}</td>
            <td>{{ b.held_count }}</td>
            <td>
              <button class="mini" @click="verify(b.batch_id)">只读再核证</button>
              <div v-if="verifyCache[b.batch_id]" style="margin-top:.35rem">
                <span class="tag" :class="verifyCache[b.batch_id].live_matches_snapshot ? 'ok' : 'bad'">
                  {{ verifyCache[b.batch_id].live_matches_snapshot ? '库内一致' : '库内不一致' }}
                </span>
                <code style="display:block; margin-top:.25rem">{{ verifyCache[b.batch_id].live_actual }}</code>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" style="margin-top:1rem; overflow:auto">
      <table>
        <thead><tr><th>事件</th><th>类型</th><th>格口</th><th>运单</th><th>时刻</th></tr></thead>
        <tbody>
          <tr v-for="e in events" :key="e.id">
            <td><code>{{ e.id }}</code></td>
            <td>{{ e.event_type }}</td>
            <td>{{ e.cell_id || '—' }}</td>
            <td>{{ e.tracking_no || '—' }}</td>
            <td>{{ e.event_ts }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.tag { padding: .1rem .45rem; border-radius: 3px; font-size: .8rem; font-family: var(--mono); }
.tag.ok { background: rgba(61, 155, 122, .22); color: var(--free); border: 1px solid var(--free); }
.tag.bad { background: rgba(196, 92, 106, .22); color: var(--over); border: 1px solid var(--over); }
button.mini { padding: .2rem .5rem; font-size: .78rem; }
.bad { color: var(--over); }
h3 code { margin-left: .5rem; font-size: .85rem; color: var(--muted); }
</style>
