import { describe, it, expect, vi, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia } from "pinia";

vi.mock("../api/config.js", () => ({
  listLlm: vi.fn().mockResolvedValue([]),
  listDb: vi.fn().mockResolvedValue([]),
}));
vi.mock("../api/auth.js", () => ({
  listUsers: vi
    .fn()
    .mockResolvedValue([{ id: 1, username: "test-user", role: "admin" }]),
}));
import ConfigView from "./ConfigView.vue";

async function render(role = "user") {
  localStorage.setItem("username", "test-user");
  localStorage.setItem("role", role);
  const accountActions = vi.fn();
  const wrapper = mount(ConfigView, {
    attachTo: document.body,
    global: {
      plugins: [createPinia()],
      provide: { accountActions },
      stubs: {
        ConfigList: true,
        LlmConfigForm: true,
        DbConfigForm: true,
        "router-link": { template: "<a><slot /></a>" },
      },
    },
  });
  await flushPromises();
  return { wrapper, accountActions };
}

afterEach(() => localStorage.clear());
describe("ConfigView categorized settings", () => {
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
    expect(wrapper.find("llm-config-form-stub").isVisible()).toBe(false);
    expect(wrapper.find("#settings-panel-databases").isVisible()).toBe(true);
    await wrapper.find("#settings-tab-models").trigger("click");
    expect(wrapper.find("llm-config-form-stub").isVisible()).toBe(true);
    await wrapper.find("#settings-tab-users").trigger("click");
    expect(wrapper.find("#settings-panel-users").isVisible()).toBe(true);
    expect(wrapper.find("#settings-panel-users").text()).toContain("test-user");
    wrapper.unmount();
  });

  it("filters categories and supports keyboard category navigation", async () => {
    const { wrapper } = await render("admin");
    await wrapper
      .find("#settings-tab-account")
      .trigger("keydown", { key: "ArrowDown" });
    expect(
      wrapper.find("#settings-tab-models").attributes("aria-selected"),
    ).toBe("true");
    await wrapper.find('[aria-label="搜索设置分类"]').setValue("数据库");
    expect(wrapper.findAll('[role="tab"]').map((x) => x.text())).toEqual([
      "数据库",
    ]);
    await wrapper.find("#settings-tab-databases").trigger("click");
    expect(wrapper.find("#settings-panel-databases").isVisible()).toBe(true);
    await wrapper
      .find('[aria-label="搜索设置分类"]')
      .trigger("keydown", { key: "Escape" });
    expect(wrapper.findAll('[role="tab"]')).toHaveLength(4);
    wrapper.unmount();
  });
});
