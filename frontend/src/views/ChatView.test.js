import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { useConfigStore } from "../stores/config.js";

vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("../api/chat.js", () => ({
  listSessions: vi.fn(),
  getSession: vi.fn(),
  streamChat: vi.fn(),
  clearChat: vi.fn(),
  renameSession: vi.fn(),
  deleteSession: vi.fn(),
  upload: vi.fn(),
  schema: vi.fn(),
}));
import * as chatApi from "../api/chat.js";
import ChatView from "./ChatView.vue";

async function render() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useConfigStore();
  store.llmList = [
    { id: 1, name: "模型一" },
    { id: 2, name: "模型二" },
  ];
  store.dbList = [{ id: 3, name: "业务库" }];
  store.currentLlmId = 1;
  store.currentDbId = 3;
  store.setSession("session-1");
  const wrapper = mount(ChatView, {
    global: {
      plugins: [pinia],
      stubs: {
        ChartView: true,
        DataTable: true,
        MarkdownContent: true,
        AgentTrace: true,
        "router-link": { template: "<a><slot/></a>" },
      },
    },
  });
  await flushPromises();
  return { wrapper, store };
}
beforeEach(() => {
  vi.resetAllMocks();
  chatApi.listSessions.mockResolvedValue([
    { id: "session-1", title: "销售分析" },
  ]);
  chatApi.getSession.mockResolvedValue({ messages: [] });
});
describe("ChatView recovered workspace", () => {
  it("places database selection in the header and model selection in the composer without submitting", async () => {
    const { wrapper, store } = await render();
    expect(wrapper.find(".analysis-workspace").exists()).toBe(true);
    expect(wrapper.find(".header-db-menu").text()).toContain("业务库");
    expect(wrapper.find(".composer-model-menu").text()).toContain("模型一");
    await wrapper.find("textarea").setValue("销售额");
    const model = wrapper.findAll(".model-menu button")[1];
    expect(model.attributes("type")).toBe("button");
    await model.trigger("click");
    expect(store.currentLlmId).toBe(2);
    expect(chatApi.streamChat).not.toHaveBeenCalled();
    expect(wrapper.text()).not.toContain("参与库");
    expect(wrapper.text()).not.toContain("新建项目");
    wrapper.unmount();
  });
  it("keeps SSE requests within the supported API and renders plan states as labelled icons", async () => {
    chatApi.streamChat.mockImplementation(async (payload, callbacks) => {
      callbacks.onPlan({
        goal: "分析销售",
        steps: [
          { id: "a", title: "准备", status: "pending" },
          { id: "b", title: "查询", status: "running" },
          { id: "c", title: "核验", status: "succeeded" },
        ],
      });
      callbacks.onDone({
        reply: "执行前需要确认",
        confirmation: { approval_id: "approval-1", status: "pending" },
      });
    });
    const { wrapper } = await render();
    await wrapper.find("textarea").setValue("分析销售");
    await wrapper.find("form.analysis-composer").trigger("submit");
    await flushPromises();
    expect(chatApi.streamChat.mock.calls[0][0]).toEqual({
      message: "分析销售",
      session_id: "session-1",
      llm_config_id: 1,
      db_config_id: 3,
      file_ids: [],
    });
    expect(wrapper.find('[aria-label="等待执行"] svg').exists()).toBe(true);
    expect(wrapper.find('[aria-label="正在执行"] svg').exists()).toBe(true);
    expect(wrapper.find('[aria-label="已执行"] svg').exists()).toBe(true);
    expect(wrapper.text()).toContain("确认执行");
    wrapper.unmount();
  });
});
