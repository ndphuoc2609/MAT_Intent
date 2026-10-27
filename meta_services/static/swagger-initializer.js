window.addEventListener("load", () => {
  window.ui = SwaggerUIBundle({
    url: "/openapi.json",
    dom_id: "#swagger-ui",
    deepLinking: true,
    displayRequestDuration: true,
    persistAuthorization: false,
    validatorUrl: null,
    supportedSubmitMethods: ["get"],
    presets: [SwaggerUIBundle.presets.apis],
    layout: "BaseLayout",
  });
});
