/**
  * 该示例内包含了【群内机器人主动在群内文本消息到群】的能力
  * 
  * 接口文档：https://open.dingtalk.com/document/orgapp/the-robot-sends-a-group-message
  */

async function sendGroupMessage(textContent) {
    // 这里的 appKey、appSecret 需要在轻研查看应用的【钉应用】 前往查看 https://mapp.alibaba-inc.com/
    const appkey = "dingkvxnfj7q1hmyznaw"; // 轻研应用中的appkey
    const appsecret = "Gmn5cK7QlIMuhRPICOsizZ1cDOolsOXQmZgi7ZJyKdx2WptgcuYxLozGrVB5dC1M"; // 轻研应用中的appsecret

    // 获取 accessToken：https://open.dingtalk.com/document/orgapp/obtain-the-access_token-of-an-internal-app
    const tokenResponse = await fetch("https://api.dingtalk.com/v1.0/oauth2/accessToken", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ appKey: appkey, appSecret: appsecret }),
    });
    if (!tokenResponse.ok) {
      const errText = await tokenResponse.text();
      throw new Error(`获取 accessToken 失败: ${tokenResponse.status} ${errText}`);
    }
    const tokenData = await tokenResponse.json();
    const access_token = tokenData.accessToken;
    console.log('access_token-----24', access_token)
    
    // 这里的robotCode 需要在轻研查看应用的【凭证信息】 前往查看 https://mapp.alibaba-inc.com/apps
    const robotCode = "dingkvxnfj7q1hmyznaw"; // 轻研机器人应用中的robotCode
  
    // 给哪个群推送，如何获取群id查看文档：https://alidocs.dingtalk.com/i/nodes/lyQod3RxJKe9QjOMiaqwxoYaWkb4Mw9r
    const openConversationId = "cid+Z9S6rV/rIu8HPYVsrsa7Q=="; 
  
    // 消息类型，使用不同的msgKey，机器人发送出来的消息样式将不一样，详见下方文档
    // https://open.dingtalk.com/document/orgapp/chatbots-send-one-on-one-chat-messages-in-batches
    // 此处仅以markdown方式举例，业务根据文档自己选择合适的 msgKey 以及对应的 msgParam
    const msgKey = 'sampleMarkdown'; 
    const msgParam = JSON.stringify({
      "title": "这段内容将显示在消息列表里。",
      "text": textContent
    });
  
    const body = {
      robotCode,
      openConversationId, 
      msgKey,
      msgParam,
    };
    // 批量发送卡片消息
    const response = await fetch("https://api.dingtalk.com/v1.0/robot/groupMessages/send", {
      method: "POST",
      headers: {
        "x-acs-dingtalk-access-token": access_token,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const errText = await response.text();
      throw new Error(`发送群消息失败: ${response.status} ${errText}`);
    }
    const data = await response.json();

    console.log(">> response：", data);
    return data;
  }

  const fs = require('fs');
  const path = require('path');

  const textContent = fs.readFileSync(
    path.join(__dirname, 'horizon-2026-06-26-zh.md'),
    'utf-8'
  );
  sendGroupMessage(textContent).catch(console.error);