# Adding a camera

Got a LUMIX body other than the DC-S5? It probably already connects on the
`generic` profile. To make it a first-class model:

1. Run the server with the generic profile and verbose logging:
   `./run.sh --camera test --model generic -v` (log in `logs/camera-test-*.log`).
2. Save the device description (`curl http://<camera-ip>:60606/Lumix/Server0/ddd`)
   and `getinfo capability`.
3. **Remove serial numbers, MAC/Bluetooth addresses, SSIDs and your own IP
   addresses** before sharing or committing anything.
4. Write a profile in `lumix/models/<id>.py`, register it, add a test.

The full field reference, a profile template and the checklist are in
[camera-models.md](camera-models.md#adding-a-model). If you can't write the
profile yourself, open an issue with the model, firmware version, the scrubbed
`ddd` XML and what worked (connect, live view, shutter, files, Bluetooth).
