// 发布队列是固定业务区标题；素材和账号始终按当前平台独立呈现。
state.activeMaterialPlatform = "douyin";

const platformTabNames = { douyin: "抖音", xiaohongshu: "小红书" };
const renderBeforePlatformTabs = render;
const optionsBeforePlatformTabs = options;
const renderSyncAccountsBeforePlatformTabs = renderSyncAccounts;

function platformMaterial(item) {
  // 历史素材未记录来源平台时仍保留在原有队列中，避免被本次改造隐藏。
  return !item.source_platform || item.source_platform === state.activeMaterialPlatform;
}

options = function platformOptions(current = "") {
  const previous = state.accounts;
  state.accounts = previous.filter((account) => account.platform === state.activeMaterialPlatform);
  const markup = optionsBeforePlatformTabs(current);
  state.accounts = previous;
  return markup;
};

renderSyncAccounts = function renderPlatformSyncAccounts() {
  const previous = state.syncAccounts;
  state.syncAccounts = previous.filter((account) => account.platform === state.activeMaterialPlatform);
  state.accountPlatform = "all";
  renderSyncAccountsBeforePlatformTabs();
  state.syncAccounts = previous;
  const filters = document.querySelector("#accountPlatformFilters");
  if (filters) filters.hidden = true;
};

function renderPlatformTabs() {
  document.querySelectorAll("[data-material-platform]").forEach((button) => {
    const active = button.dataset.materialPlatform === state.activeMaterialPlatform;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
}

render = function renderCurrentPlatform() {
  const allItems = state.items;
  const allAccounts = state.accounts;
  state.items = allItems.filter(platformMaterial);
  state.accounts = allAccounts.filter((account) => account.platform === state.activeMaterialPlatform);
  renderBeforePlatformTabs();
  state.items = allItems;
  state.accounts = allAccounts;
  renderPlatformTabs();
  renderSyncAccounts();
};

document.querySelector("#platformTabs")?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-material-platform]");
  if (!button || button.dataset.materialPlatform === state.activeMaterialPlatform) return;
  state.activeMaterialPlatform = button.dataset.materialPlatform;
  state.filter = "queued";
  state.publishedAccountKey = "";
  document.querySelector("#selectAll").checked = false;
  render();
});

// 先等已有的素材和账号异步加载完成，再以抖音素材作为默认入口渲染。
Promise.resolve().then(() => render());
