from .srvehiclehub import SRVehicleHub

async def setup(bot):
    await bot.add_cog(SRVehicleHub(bot))
