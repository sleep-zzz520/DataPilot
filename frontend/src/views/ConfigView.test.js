import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises, DOMWrapper } from "@vue/test-utils";
import { createPinia } from "pinia";

vi.mock("../api/config.js", () => ({
  listLlm: vi.fn().mockResolvedValue([]),
  listDb: vi.fn().mockResolvedValue([]),
  saveLlm: vi.fn(),
  testLlm: vi.fn().mockResolvedValue({ ok: true }),
  saveDb: vi.fn(),
  testDb: vi.fn().mockResolvedValue({ ok: true }),
}));
vi.mock("../api/auth.js", () => ({
  me: vi.fn(),
  listUsers: vi
    .fn()
    .mockResolvedValue([{ id: 1, username: "test-user", role: "admin" }]),
}));
import ConfigView from "./ConfigView.vue";
import * as authApi from "../api/auth.js";
import * as configApi from "../api/config.js";

beforeEach(() => {
  configApi.listLlm.mockResolvedValue([]);
  configApi.listDb.mockResolvedValue([]);
  configApi.testLlm.mockResolvedValue({ ok: true });
  configApi.testDb.mockResolvedValue({ ok: true });
  authApi.listUsers.mockResolvedValue([{ id: 1, username: "test-user", role: "admin" }]);
});

async function render(role = "user", serverRole = role, realModelForm = false, realDbForm = false) {
  localStorage.setItem("username", "test-user");
  localStorage.setItem("role", role);
  if (serverRole instanceof Error) authApi.me.mockRejectedValueOnce(serverRole);
  else authApi.me.mockResolvedValueOnce({ id: 1, username: "test-user", role: serverRole });
  const accountActions = vi.fn();
  const wrapper = mount(ConfigView, {
    attachTo: document.body,
    global: {
      plugins: [createPinia()],
      provide: { accountActions },
      stubs: {
        ConfigList: true,
        LlmConfigForm: !realModelForm,
        DbConfigForm: !realDbForm,
        "router-link": { template: "<a><slot /></a>" },
      },
    },
  });
  await flushPromises();
  return { wrapper, accountActions };
}

afterEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});
describe("ConfigView categorized settings", () => {
  it("shows model creation after a server-side promotion despite a stale user cache", async () => {
    const { wrapper } = await render("user", "admin");
    expect(wrapper.find('[aria-label="账户设置"]').text()).toContain("管理员");
    expect(localStorage.getItem("role")).toBe("admin");
    expect(wrapper.find("#settings-tab-users").exists()).toBe(true);
    await wrapper.find("#settings-tab-models").trigger("click");
    const button = wrapper.find(".settings-content-heading .primary-button");
    expect(button.text()).toContain("添加模型");
    await button.trigger("click");
    expect(wrapper.find("llm-config-form-stub").isVisible()).toBe(true);
    wrapper.unmount();
  });

  it("removes model management after a server-side demotion despite an admin cache", async () => {
    const { wrapper } = await render("admin", "user");
    expect(localStorage.getItem("role")).toBe("user");
    expect(wrapper.find("#settings-tab-users").exists()).toBe(false);
    await wrapper.find("#settings-tab-models").trigger("click");
    expect(wrapper.find(".settings-content-heading .primary-button").exists()).toBe(false);
    expect(wrapper.findComponent({ name: "ConfigList" }).props("readonly")).toBe(true);
    wrapper.unmount();
  });

  it("does not grant cached admin controls when current-account verification fails", async () => {
    const { wrapper } = await render("admin", new Error("无法确认当前账号"));
    expect(wrapper.find('[role="alert"]').text()).toContain("无法确认当前账号");
    expect(wrapper.find("#settings-tab-users").exists()).toBe(false);
    await wrapper.find("#settings-tab-models").trigger("click");
    expect(wrapper.find(".settings-content-heading .primary-button").exists()).toBe(false);
    wrapper.unmount();
  });

  it("lets ordinary users access their own account actions without user management", async () => {
    const { wrapper, accountActions } = await render();
    const account = wrapper.find('[aria-label="账户设置"]');
    expect(account.text()).toContain("test-user");
    expect(account.text()).toContain("普通用户");
    expect(wrapper.find("#settings-tab-users").exists()).toBe(false);
    await account
      .findAll("button")
      .find((b) => b.text() === "修改密码")
      .trigger("click");
    expect(accountActions).toHaveBeenLastCalledWith("password");
    await account
      .findAll("button")
      .find((b) => b.text() === "注销账号")
      .trigger("click");
    expect(accountActions).toHaveBeenLastCalledWith("delete");
    wrapper.unmount();
  });

  it("shows one category at a time and opens configuration forms only on demand", async () => {
    const { wrapper } = await render("admin");
    expect(wrapper.find("#settings-panel-account").isVisible()).toBe(true);
    expect(wrapper.find("#settings-panel-models").isVisible()).toBe(false);
    await wrapper.find("#settings-tab-models").trigger("click");
    expect(wrapper.find("#settings-panel-account").isVisible()).toBe(false);
    expect(wrapper.find("#settings-panel-models").isVisible()).toBe(true);
    expect(wrapper.find("llm-config-form-stub").exists()).toBe(false);
    await wrapper
      .find(".settings-content-heading .primary-button")
      .trigger("click");
    expect(wrapper.find("llm-config-form-stub").isVisible()).toBe(true);
    await wrapper.find("#settings-tab-databases").trigger("click");
    expect(wrapper.find("llm-config-form-stub").exists()).toBe(false);
    expect(wrapper.find("#settings-panel-databases").isVisible()).toBe(true);
    await wrapper.find("#settings-tab-models").trigger("click");
    expect(wrapper.find("llm-config-form-stub").exists()).toBe(false);
    await wrapper.find("#settings-tab-users").trigger("click");
    expect(wrapper.find("#settings-panel-users").isVisible()).toBe(true);
    expect(wrapper.find("#settings-panel-users").text()).toContain("test-user");
    wrapper.unmount();
  });

  it("opens a modal and resets the draft after each supported dismissal", async () => {
    const { wrapper } = await render("admin", "admin", true);
    await wrapper.find("#settings-tab-models").trigger("click");
    const add = wrapper.find(".settings-content-heading .primary-button");
    const dialog = wrapper.find("dialog");
    expect(dialog.element.open).toBe(false);
    for (const dismiss of ["close", "cancel-button", "escape", "backdrop"]) {
      await add.trigger("click");
      await flushPromises();
      expect(dialog.element.open).toBe(true);
      expect(dialog.attributes("aria-labelledby")).toBe("model-dialog-title");
      const name = wrapper.find('input[placeholder="例如：GPT-4"]');
      expect(name.element.value).toBe("");
      await name.setValue("未保存的草稿");
      await wrapper.find(".model-dialog-body").trigger("click");
      expect(dialog.element.open).toBe(true);
      if (dismiss === "close") await wrapper.find('[aria-label="关闭模型弹窗"]').trigger("click");
      else if (dismiss === "cancel-button") await wrapper.find(".model-dialog .cancel").trigger("click");
      else if (dismiss === "escape") await dialog.trigger("cancel");
      else await dialog.trigger("click");
      await flushPromises();
      expect(dialog.element.open).toBe(false);
      expect(wrapper.find(".llm-config-form").exists()).toBe(false);
    }
    wrapper.unmount();
  });

  it("blocks duplicate saves and dismissal until the save completes, then closes", async () => {
    vi.spyOn(window, "alert").mockImplementation(() => {});
    let completeSave;
    configApi.saveLlm.mockImplementationOnce(() => new Promise(resolve => { completeSave = resolve; }));
    const { wrapper } = await render("admin");
    await wrapper.find("#settings-tab-models").trigger("click");
    await wrapper.find(".settings-content-heading .primary-button").trigger("click");
    await flushPromises();
    const form = wrapper.findComponent({ name: "LlmConfigForm" });
    const payload = { name: "测试配置", provider: "openai", api_key: "unit-test-placeholder", model_name: "test-model" };
    form.vm.$emit("submit", payload);
    form.vm.$emit("submit", payload);
    await flushPromises();
    expect(configApi.saveLlm).toHaveBeenCalledTimes(1);
    expect(form.props("saving")).toBe(true);
    await wrapper.find("dialog").trigger("cancel");
    expect(wrapper.find("dialog").element.open).toBe(true);
    completeSave({ ...payload, id: 88, api_key_masked: "****" });
    await flushPromises();
    expect(configApi.testLlm).toHaveBeenCalledTimes(1);
    expect(wrapper.find("dialog").element.open).toBe(false);
    wrapper.unmount();
  });

  it("keeps the draft open when saving fails", async () => {
    vi.spyOn(window, "alert").mockImplementation(() => {});
    configApi.saveLlm.mockRejectedValueOnce(new Error("保存失败"));
    const { wrapper } = await render("admin", "admin", true);
    await wrapper.find("#settings-tab-models").trigger("click");
    await wrapper.find(".settings-content-heading .primary-button").trigger("click");
    await wrapper.find('input[placeholder="例如：GPT-4"]').setValue("待重试模型");
    await wrapper.find(".llm-config-form form").trigger("submit");
    await flushPromises();
    expect(wrapper.find("dialog").element.open).toBe(true);
    expect(wrapper.find('input[placeholder="例如：GPT-4"]').element.value).toBe("待重试模型");
    expect(wrapper.find(".submit").element.disabled).toBe(false);
    wrapper.unmount();
  });

  it("uses the same dialog for editing and preserves the model id and default flag", async () => {
    vi.spyOn(window, "alert").mockImplementation(() => {});
    const existing = { id: 88, name: "现有模型", provider: "openai", model_name: "test-model", is_default: true };
    configApi.saveLlm.mockResolvedValueOnce(existing);
    const { wrapper } = await render("admin");
    await wrapper.find("#settings-tab-models").trigger("click");
    wrapper.findComponent({ name: "ConfigList" }).vm.$emit("select", existing);
    await flushPromises();
    expect(wrapper.find("dialog").element.open).toBe(true);
    expect(wrapper.find("#model-dialog-title").text()).toBe("编辑模型");
    wrapper.findComponent({ name: "LlmConfigForm" }).vm.$emit("submit", { name: "修改模型", provider: "openai", model_name: "test-model" });
    await flushPromises();
    expect(configApi.saveLlm).toHaveBeenCalledWith(expect.objectContaining({ id: 88, is_default: true }));
    expect(wrapper.find("dialog").element.open).toBe(false);
    wrapper.unmount();
  });

  it("opens the database form in a modal and resets its draft after dismissal", async () => {
    const { wrapper } = await render("admin", "admin", false, true);
    await wrapper.find("#settings-tab-databases").trigger("click");
    const add = wrapper.find(".settings-content-heading .primary-button");
    const dialog = wrapper.find(".database-dialog");
    expect(dialog.element.open).toBe(false);
    for (const dismiss of ["close", "cancel-button", "escape", "backdrop"]) {
      await add.trigger("click");
      await flushPromises();
      expect(dialog.element.open).toBe(true);
      expect(wrapper.find("#database-dialog-title").text()).toContain("添加数据库");
      expect(wrapper.find("#settings-panel-databases .db-config-form").exists()).toBe(false);
      const name = dialog.find('input[placeholder="例如：生产数据库"]');
      expect(name.element.value).toBe("");
      await name.setValue("数据库草稿");
      await dialog.find(".model-dialog-body").trigger("click");
      expect(dialog.element.open).toBe(true);
      if (dismiss === "close") await dialog.find('[aria-label="关闭数据库弹窗"]').trigger("click");
      else if (dismiss === "cancel-button") await dialog.find(".cancel").trigger("click");
      else if (dismiss === "escape") await dialog.trigger("cancel");
      else await dialog.trigger("click");
      await flushPromises();
      expect(dialog.element.open).toBe(false);
      expect(wrapper.find(".db-config-form").exists()).toBe(false);
    }
    wrapper.unmount();
  });

  it.each([true, false])("saves a database once and closes after connection test success=%s", async (testSuccess) => {
    vi.spyOn(window, "alert").mockImplementation(() => {});
    let completeSave;
    configApi.saveDb.mockImplementationOnce(() => new Promise(resolve => { completeSave = resolve; }));
    if (!testSuccess) configApi.testDb.mockRejectedValueOnce(new Error("连接失败"));
    const { wrapper } = await render("admin");
    await wrapper.find("#settings-tab-databases").trigger("click");
    await wrapper.find(".settings-content-heading .primary-button").trigger("click");
    await flushPromises();
    const dialog = wrapper.find(".database-dialog");
    const form = wrapper.findComponent({ name: "DbConfigForm" });
    const payload = { name: "测试库", host: "localhost", port: 3306, db_type: "mysql", username: "unit-test-user", database: " demo " };
    form.vm.$emit("submit", payload);
    form.vm.$emit("submit", payload);
    await flushPromises();
    expect(configApi.saveDb).toHaveBeenCalledTimes(1);
    expect(configApi.saveDb).toHaveBeenCalledWith(expect.objectContaining({ default_schema: "demo", is_default: false }));
    expect(form.props("saving")).toBe(true);
    expect(dialog.find('[aria-label="关闭数据库弹窗"]').element.disabled).toBe(true);
    await dialog.trigger("cancel");
    await dialog.trigger("click");
    expect(dialog.element.open).toBe(true);
    completeSave({ ...payload, id: 77, default_schema: "demo", password_masked: "****" });
    await flushPromises();
    expect(configApi.testDb).toHaveBeenCalledTimes(1);
    expect(dialog.element.open).toBe(false);
    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining(testSuccess ? "连接测试通过" : "配置已保存，但连接测试失败"));
    wrapper.unmount();
  });

  it("keeps the database draft open when saving fails", async () => {
    vi.spyOn(window, "alert").mockImplementation(() => {});
    configApi.saveDb.mockRejectedValueOnce(new Error("保存失败"));
    const { wrapper } = await render("admin", "admin", false, true);
    await wrapper.find("#settings-tab-databases").trigger("click");
    await wrapper.find(".settings-content-heading .primary-button").trigger("click");
    const dialog = wrapper.find(".database-dialog");
    await dialog.find('input[placeholder="例如：生产数据库"]').setValue("待重试数据库");
    await dialog.find("form").trigger("submit");
    await flushPromises();
    expect(dialog.element.open).toBe(true);
    expect(dialog.find('input[placeholder="例如：生产数据库"]').element.value).toBe("待重试数据库");
    expect(dialog.find(".submit").element.disabled).toBe(false);
    expect(configApi.testDb).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("edits a database in the same modal while preserving its id, charset and default flag", async () => {
    vi.spyOn(window, "alert").mockImplementation(() => {});
    const existing = { id: 77, name: "现有数据库", host: "localhost", port: 3306, db_type: "mysql", username: "unit-test-user", charset: "gbk", default_schema: "analytics", is_default: true };
    configApi.saveDb.mockResolvedValueOnce(existing);
    const { wrapper } = await render("admin", "admin", false, true);
    await wrapper.find("#settings-tab-databases").trigger("click");
    wrapper.findAllComponents({ name: "ConfigList" })[1].vm.$emit("select", existing);
    await flushPromises();
    const dialog = wrapper.find(".database-dialog");
    expect(dialog.element.open).toBe(true);
    expect(wrapper.find("#database-dialog-title").text()).toContain("编辑数据库");
    expect(dialog.find('input[placeholder="mydb"]').element.value).toBe("analytics");
    await dialog.find('input[placeholder="mydb"]').setValue("warehouse");
    await dialog.find("form").trigger("submit");
    await flushPromises();
    expect(configApi.saveDb).toHaveBeenCalledWith(expect.objectContaining({ id: 77, default_schema: "warehouse", charset: "gbk", is_default: true }));
    expect(dialog.element.open).toBe(false);
    wrapper.unmount();
  });

  it("searches categories in a dialog while preserving the full sidebar and keyboard navigation", async () => {
    const { wrapper } = await render("admin");
    await wrapper
      .find("#settings-tab-account")
      .trigger("keydown", { key: "ArrowDown" });
    expect(wrapper.find("#settings-tab-models").attributes("aria-selected")).toBe("true");
    await wrapper.find(".settings-category-search").trigger("click");
    await flushPromises();
    const dialog = new DOMWrapper(document.getElementById("settings-search"));
    expect(dialog.element.open).toBe(true);
    await dialog.find("input").setValue("数据库");
    expect(wrapper.findAll('[role="tab"]')).toHaveLength(4);
    const result = dialog.findAll("button").find(button => button.text() === "数据库");
    expect(result).toBeDefined();
    expect(dialog.findAll("button").some(button => button.text() === "用户管理")).toBe(false);
    await result.trigger("click");
    await flushPromises();
    expect(dialog.element.open).toBe(false);
    expect(wrapper.find("#settings-panel-databases").isVisible()).toBe(true);
    await wrapper.find("#settings-tab-databases").trigger("keydown", { key: "End" });
    expect(wrapper.find("#settings-tab-users").attributes("aria-selected")).toBe("true");
    wrapper.unmount();
  });

  it("excludes user management from ordinary users' search results", async () => {
    const { wrapper } = await render();
    await wrapper.find(".settings-category-search").trigger("click");
    await flushPromises();
    const dialog = new DOMWrapper(document.getElementById("settings-search"));
    expect(dialog.findAll("button").some(button => button.text() === "用户管理")).toBe(false);
    await dialog.find("input").setValue("用户");
    expect(dialog.find('[role="status"]').text()).toBe("没有匹配的结果");
    expect(wrapper.findAll('[role="tab"]')).toHaveLength(3);
    await dialog.trigger("cancel");
    await flushPromises();
    expect(dialog.element.open).toBe(false);
    expect(wrapper.find("#settings-panel-account").isVisible()).toBe(true);
    wrapper.unmount();
  });
});
