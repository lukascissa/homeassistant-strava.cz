to configuration.yml add sensor definition like:

```
sensor:
  - platform: stravacz
    name: "Jídelna – Prcek"
    # LOGIN
    login_cislo: "123"
    login_jmeno: "prcek"
    login_heslo: "prckovoheslo"
    environment: "W"
    lang: "EN"
    # ORDERS
    objednavky_cislo: "123"
    konto: 0
    s5url: "https://wss52.strava.cz/WSStravne5_8/WSStravne5.svc"
    days: 5
```
